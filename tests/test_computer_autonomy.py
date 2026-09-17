"""Synthetic computer data, explicit communication choices and owner help."""

import json
import sys

import pytest
from test_kin_mind import FakeReviewer, wish
from test_kin_mind import setup as _setup

from eventmem.core.db import Conflict
from kin_mind.actions import ActionEvents
from kin_mind.appraisal import Appraisal, Appraisals, Wish
from kin_mind.computer import ComputerReader, create_server, redact
from kin_mind.continuity import ConcernChange, ContinuityConfig, OwnerRequest
from kin_mind.exploration import Explorations, final_result, run_kimi
from kin_mind.exploration_decisions import SharingDecision
from kin_mind.state import Mind

setup = _setup


def result(*args, **kwargs):
    return {"state": "complete", "partial": False, "provider": "kimi-cli", "result": {
        "summary": "A useful synthetic finding", "findings": ["The document discusses a draft"],
        "sources": [{"url": "https://example.com/draft", "title": "Synthetic document"}],
        "open_questions": [], "suggested_share": "An optional suggestion"}}


def explored(setup, tmp_path):
    mind, source, clock = setup
    wish(mind, source, "investigate", kind="explore", content="Understand a synthetic document")
    value = Explorations(mind).run("fake", tmp_path / "jobs", "test", runner=result)
    jobs = Appraisals(mind, exploration_capabilities={"decisions": True})
    ActionEvents(mind).drain(jobs)
    return mind, source, clock, value, jobs


@pytest.mark.parametrize("decision", ["keep", "defer", "share"])
def test_result_decision_survives_restart_without_auto_send(setup, tmp_path, decision):
    mind, _, _, value, jobs = explored(setup, tmp_path)
    share = SharingDecision(exploration_id=value["id"], decision=decision, reason="A deliberate choice",
                            reconsider_when="New relevant owner feedback" if decision == "defer" else None)
    wishes = [Wish(content="Discuss this document", topic="draft", kind="contact", strength=85,
                   ttl_hours=24, completion="Deliver the thought")] if decision == "share" else []
    reviewer = FakeReviewer(Appraisal(reason="A result does not require conversation", sharing=[share], wishes=wishes))
    assert jobs.run_one(reviewer)["state"] == "complete"
    view = Mind(mind.engine, mind.scope).read()
    assert view["exploration_decisions"][0]["decision"] == decision
    assert len([d for d in view["desires"] if d["kind"] == "contact"]) == (decision == "share")
    assert jobs.run_one(reviewer)["state"] == "idle"


@pytest.mark.parametrize("isolated", [False, True])
def test_invalid_keep_plus_contact_rolls_back_everything(setup, tmp_path, isolated):
    """Sections are isolated by default since stage 1 (A1): the wish that contradicts `keep` is refused alone and
    the host-validated decision stands. With the switch off the previous contract holds: everything rolls back.
    Either way the contact wish never leaks."""
    from kin_mind.memory import MemoryContinuity

    mind, _, _, value, jobs = explored(setup, tmp_path)
    if not isolated:
        MemoryContinuity(mind).configure({"appraisal_section_isolation": False})
    before = mind.read()["revision"]
    reviewer = FakeReviewer(Appraisal(values={"initiative": 99}, reason="Invalid mixed result",
        sharing=[SharingDecision(exploration_id=value["id"], decision="keep", reason="Keep it")],
        wishes=[Wish(content="Must not leak", topic="draft", kind="contact", strength=99, ttl_hours=24, completion="send")]))
    if isolated:
        outcome = jobs.run_one(reviewer)
        assert outcome["state"] == "complete" and outcome["result"]["rejected_sections"] == [
            {"section": "wishes", "code": "conflict", "message": "This result has no current decision to communicate"}]
        view = mind.read()
        assert view["revision"] == before + 1 and view["exploration_decisions"][0]["decision"] == "keep"
        assert [d for d in view["desires"] if d["kind"] == "contact"] == [] and "Must not leak" not in json.dumps(view["desires"])
        assert not mind.contact_candidate()["eligible"]
        return
    assert jobs.run_one(reviewer)["state"] == "pending"
    assert mind.read()["revision"] == before
    assert mind.read()["exploration_decisions"] == []


def test_clock_cannot_reopen_keep_but_new_feedback_can(setup, tmp_path):
    mind, source, _, value, jobs = explored(setup, tmp_path)
    keep = SharingDecision(exploration_id=value["id"], decision="keep", reason="Only remember")
    assert jobs.run_one(FakeReviewer(Appraisal(reason="Keep", sharing=[keep])))["state"] == "complete"
    share = SharingDecision(exploration_id=value["id"], decision="share", reason="Now relevant")
    jobs.enqueue([source("clock")], "test", origin="reflection", stimulus="drive-crossing")
    assert jobs.run_one(FakeReviewer(Appraisal(reason="Clock", sharing=[share])))["state"] == "pending"
    jobs.enqueue([source("owner asks about the draft")], "test")
    assert jobs.run_one(FakeReviewer(Appraisal(reason="Owner feedback", sharing=[share])))["state"] == "complete"
    assert mind.read()["exploration_decisions"][0]["decision"] == "share"


def test_missing_decision_and_invalid_defer_are_not_silent_acceptance(setup, tmp_path):
    _, _, _, value, jobs = explored(setup, tmp_path)
    outcome = jobs.run_one(FakeReviewer(Appraisal(reason="Missing decision")))
    assert outcome["state"] == "pending" and outcome["error"] == "deepseek-missing-sharing-decision"
    with pytest.raises(ValueError):
        SharingDecision(exploration_id=value["id"], decision="defer", reason="Later")


@pytest.mark.parametrize("request_kind", ["help", "invitation", "request"])
def test_help_is_not_accepted_or_completed_by_delivery(setup, request_kind):
    mind, source, _ = setup
    mind.configure_continuity(ContinuityConfig(command_id="enable", agent_version="test",
        expected_revision=mind.read()["revision"], evidence_ids=[source("enable")],
        features={k: True for k in ("interpretation", "concerns", "expression", "rhythm")}, reason="Enable"))
    help_request = OwnerRequest(kind=request_kind, action="Photograph the object", reason="I want to see its design",
                               completion="A readable label photo arrives")
    created = mind.manage_concern(ConcernChange(command_id="help", agent_version="test",
        expected_revision=mind.read()["revision"], evidence_ids=[source("idea", authority="model")],
        action="create", key="label-help", kind="curiosity", content="Understand the label", topic="label",
        intensity=75, basis="internal_thought", confidence=1, reason="Ask for a real-world clue", owner_request=help_request))
    cid = created["concern_id"]
    wish(mind, source, "ask", concern_ids=[cid])
    assert mind.read(query="label")["selected_concerns"][0]["owner_request"]["status"] == "proposed"
    with pytest.raises(Conflict):
        mind.manage_concern(ConcernChange(command_id="fake-accept", agent_version="test",
            expected_revision=mind.read()["revision"], evidence_ids=[source("delivery", authority="model")],
            action="update", concern_id=cid, owner_request=help_request.model_copy(update={"status": "accepted"}), reason="Delivery is not assent"))
    for status in ("waiting", "accepted", "completed"):
        mind.manage_concern(ConcernChange(command_id=status, agent_version="test",
            expected_revision=mind.read()["revision"], evidence_ids=[source(status)], concern_id=cid,
            action="resolve" if status == "completed" else "update",
            owner_request=help_request.model_copy(update={"status": status}), reason="Actual owner response"))
    assert mind.read()["concerns"][0]["status"] == "resolved"
    assert mind.read()["concerns"][0]["owner_request"]["status"] == "completed"


def test_filtered_file_reads_deduplicate_and_do_not_claim_authorship(tmp_path):
    path = tmp_path / "draft.txt"
    path.write_text('A draft\napi_key="sk-synthetic_private_123456789"')
    reader = ComputerReader({"roots": [str(tmp_path)], "ledger": str(tmp_path / "ledger.json")})
    first = reader.read_resource(path)
    second = reader.read_resource(path)
    assert first["id"] == second["id"] and len(json.loads(reader.ledger.read_text())) == 1
    assert "sk-synthetic" not in second["text"] and second["actor"] == "unknown"
    assert reader.read_resource(path, offset=2)["id"] != first["id"]
    path.write_text("A revised draft")
    assert reader.read_resource(path)["version"] != first["version"]
    secret = tmp_path / ".env"
    secret.write_text("SECRET=x")
    with pytest.raises(ValueError): reader.read_resource(secret)
    link = tmp_path / "innocent.txt"
    link.symlink_to(secret)
    with pytest.raises(ValueError): reader.read_resource(link)
    assert ".env" not in str(reader.list_files(tmp_path))
    assert "secret" not in redact("https://user:secret@example.com/?token=secret")


def test_snapshot_is_on_demand_and_carries_no_clock_in_identity(tmp_path):
    adapter = tmp_path / "snapshot.py"
    adapter.write_text('import json\nprint(json.dumps({"application":"Synthetic","window":"A draft"}))\n')
    cfg = {"roots": [str(tmp_path)], "ledger": str(tmp_path / "ledger.json"), "snapshot_command": [sys.executable, str(adapter)]}
    reader = ComputerReader(cfg)
    assert not reader.ledger.exists()
    first = reader.context()
    assert first["state"] == "observed"
    cfg["previous"] = [first["observation"]]
    assert ComputerReader(cfg).context()["observation"]["changed_since_last_observation"] is False
    tools = create_server(cfg)._tool_manager.list_tools()
    assert {t.name for t in tools} == {"read_computer_context", "list_computer_files", "read_computer_resource"}


def test_computer_exploration_must_be_enabled_and_keeps_old_default(setup, tmp_path):
    mind, source, _ = setup
    wish(mind, source, "computer", kind="explore", exploration_target="computer")
    explorer = Explorations(mind)
    assert explorer.run("fake", tmp_path / "jobs", "test", runner=result)["reason"] == "computer-exploration-disabled"
    def runner(*args, **kwargs):
        assert kwargs["computer"]["enabled"] and kwargs["budget_seconds"] == 1200
        return result()
    value = explorer.run("fake", tmp_path / "jobs", "test", runner=runner, computer={"enabled": True})
    assert value["exploration_target"] == "computer"


def test_computer_profile_only_exposes_filtered_tools_and_optional_help(tmp_path):
    payload = result()["result"] | {"assistance_needed": {"action": "Choose a name", "reason": "Preference matters", "completion": "One name"}}
    fake = tmp_path / "kimi"
    fake.write_text('#!/usr/bin/env python3\nimport json,sys,os\nfrom pathlib import Path\n'
        'profile=Path(sys.argv[sys.argv.index("--agent-file")+1]).read_text()\n'
        'assert "  - Read\\n" not in profile and "mcp__kin_computer__read_computer_context" in profile\n'
        'assert Path(".kimi-code/mcp.json").exists()\n'
        'assert Path(os.environ["KIMI_CODE_HOME"]).resolve()==Path(".kimi-code").resolve()\n'
        'print(json.dumps({"role":"assistant","content":'+repr(json.dumps(payload))+'}))\n')
    fake.chmod(0o700)
    value = run_kimi(fake, {"question": "synthetic"}, tmp_path / "job", computer={"enabled": True, "roots": [str(tmp_path)], "kimi_home": str(tmp_path / "synthetic-login")})
    assert value["state"] == "complete" and value["result"]["assistance_needed"]["action"] == "Choose a name"
    assert final_result(json.dumps({"role": "assistant", "content": [{"type": "thinking", "thinking": json.dumps(payload)}]})) is None


def test_resource_identity_survives_short_context_window(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("A stable synthetic note")
    cfg = {"roots": [str(tmp_path)], "seen_database": str(tmp_path / "seen.sqlite")}
    first = ComputerReader({**cfg, "ledger": str(tmp_path / "first" / "ledger.json")}).read_resource(path)
    again = ComputerReader({**cfg, "ledger": str(tmp_path / "second" / "ledger.json"), "previous": []}).read_resource(path)
    assert first["changed_since_last_observation"] and not again["changed_since_last_observation"]
    path.write_text("A new synthetic note")
    changed = ComputerReader({**cfg, "ledger": str(tmp_path / "third" / "ledger.json")}).read_resource(path)
    assert changed["changed_since_last_observation"] and changed["version"] != first["version"]


def test_share_has_one_contact_per_review_and_source_correction_invalidates_it(setup, tmp_path):
    from test_kin_mind import event
    mind, source, _clock, value, jobs = explored(setup, tmp_path)
    share = SharingDecision(exploration_id=value["id"], decision="share", reason="Discuss the finding")
    contact = Wish(content="Discuss this finding", topic="draft", kind="contact", strength=90, ttl_hours=24, completion="send", exploration_id=value["id"])
    assert jobs.run_one(FakeReviewer(Appraisal(reason="Share", sharing=[share], wishes=[contact])))["state"] == "complete"
    mind.record(event(mind, source, "rise", {"initiative": 95}))
    ActionEvents(mind).drain(jobs)
    while jobs.run_one(FakeReviewer(Appraisal(reason="No new action", sharing=[share])))["state"] == "complete":
        pass
    ActionEvents(mind).drain(jobs)
    attempt = mind.claim_contact(owner_epoch="same")
    assert mind.check_contact(attempt["id"], "same")["eligible"]
    # Persistent high motivation cannot produce another contact for this decision.
    jobs.enqueue([source("drive")], "test", origin="reflection", stimulus="drive-crossing")
    assert jobs.run_one(FakeReviewer(Appraisal(reason="Same high score", sharing=[share], wishes=[contact.model_copy(update={"content": "Paraphrased duplicate"})])))["state"] == "complete"
    assert len([d for d in mind.read()["desires"] if d["kind"] == "contact"]) == 1
    jobs.enqueue([source("owner says no need to discuss this")], "test")
    assert jobs.run_one(FakeReviewer(Appraisal(reason="Changed relevance", sharing=[share.model_copy(update={"decision": "keep"})])))["state"] == "complete"
    assert not mind.check_contact(attempt["id"], "same")["eligible"]


def test_observation_correction_marks_result_decision_for_review(setup, tmp_path):
    from datetime import timedelta

    from eventmem.core.models import SourceInput
    mind, source, clock = setup
    wish(mind, source, "investigate", kind="explore", exploration_target="computer")
    observation = {"id": "computer_synthetic", "locator": "/synthetic/note.txt", "title": "Note", "version": "v1",
                   "observed_at": clock[0].isoformat(), "actor": "unknown", "basis": "observed", "excerpt": "A draft"}
    value = Explorations(mind).run("fake", tmp_path / "jobs", "test", computer={"enabled": True},
        runner=lambda *a, **kw: result() | {"observations": [observation]})
    jobs = Appraisals(mind, exploration_capabilities={"decisions": True})
    jobs.enqueue([value["source_id"]], "test", stimulus="exploration-result", origin="exploration")
    share = SharingDecision(exploration_id=value["id"], decision="share", reason="A sourced result")
    assert jobs.run_one(FakeReviewer(Appraisal(reason="Review", sharing=[share])))["state"] == "complete"
    assert not mind.read()["exploration_decisions"][0]["needs_review"]
    clock[0] += timedelta(seconds=1)
    mind.engine.receive(SourceInput(namespace="kin-computer-observation", key="computer_synthetic", version="2", scope=mind.scope,
        text="The earlier attribution was incorrect", occurred_at=clock[0].isoformat(), extract=False))
    assert mind.read()["exploration_decisions"][0]["needs_review"]


def test_missing_condition_keeps_the_same_exploration_wish_for_owner_followup(setup, tmp_path):
    from kin_mind.appraisal import WishUpdate
    mind, source, _ = setup
    requested = wish(mind, source, "read-label", kind="explore", exploration_target="computer")
    def blocked(*a, **kw):
        out = result()
        out["result"]["assistance_needed"] = {"action":"Photograph the label", "reason":"It cannot be read", "completion":"A legible photo"}
        return out
    first = Explorations(mind).run("fake", tmp_path / "jobs", "test", runner=blocked, computer={"enabled": True})
    desire = next(d for d in mind.read()["desires"] if d["id"] == requested["desire_id"])
    assert desire["status"] == "waiting" and desire["contact_wait"]["condition"] == "new_evidence"
    jobs = Appraisals(mind, exploration_capabilities={"decisions":True})
    events = ActionEvents(mind);events.drain(jobs)
    assert jobs.run_one(FakeReviewer(Appraisal(reason="Waiting for a condition", sharing=[SharingDecision(exploration_id=first["id"], decision="keep", reason="Keep the result for now")])))["state"] == "complete"
    events.drain(jobs)
    jobs.enqueue([source("owner supplies the label")], "test")
    assert jobs.run_one(FakeReviewer(Appraisal(reason="Condition arrived", wish_updates=[WishUpdate(desire_id=desire["id"], action="resume", reason="The owner supplied the readable label")])))["state"] == "complete"
    second = Explorations(mind).run("fake", tmp_path / "jobs", "test", runner=result, computer={"enabled": True})
    assert second["state"] == "complete" and second["desire_id"] == first["desire_id"]
    assert second["id"] != first["id"]


def test_old_intent_cannot_join_an_already_reserved_sharing_revision(setup, tmp_path):
    from kin_mind.state import DesireChange
    mind, source, _, value, jobs = explored(setup, tmp_path)
    share = SharingDecision(exploration_id=value["id"], decision="share", reason="Relevant")
    contact = Wish(content="First topic", topic="draft", kind="contact", strength=90, ttl_hours=24, completion="send", exploration_id=value["id"])
    assert jobs.run_one(FakeReviewer(Appraisal(reason="First", sharing=[share], wishes=[contact])))["state"] == "complete"
    old = next(d for d in mind.read()["desires"] if d["kind"] == "contact")
    jobs.enqueue([source("new related owner feedback")], "test")
    assert jobs.run_one(FakeReviewer(Appraisal(reason="A new related angle", sharing=[share], wishes=[contact.model_copy(update={"content":"Second topic"})])))["state"] == "complete"
    with pytest.raises(Conflict, match="another contact intent"):
        mind.manage_desire(DesireChange(command_id="rejoin", agent_version="test", expected_revision=mind.read()["revision"],
            evidence_ids=[source("resume older topic")], action="resume", desire_id=old["id"], reason="Would duplicate the reserved revision"))
