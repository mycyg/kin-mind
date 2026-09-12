from concurrent.futures import ThreadPoolExecutor

import pytest

from eventmem.core.handoffs import HandoffConflict, TaskHandoffs


@pytest.fixture
def hosts(tmp_path):
    scope = {
        "project": "example",
        "persona": "companion",
        "collection": "default",
        "world": "test",
    }
    return {
        role: TaskHandoffs(tmp_path / "exchange.sqlite3", scope, role)
        for role in ["mobile", "desktop", "transport"]
    }


def submit(hosts, command="request"):
    return hosts["mobile"].submit(
        command_id=command,
        recipient="desktop",
        session_id="shared-session",
        title="Create a note",
        goal="Write an example note",
        source="owner-input:example",
        acceptance="The note exists and can be read",
    )


def claim(hosts, task):
    return hosts["desktop"].claim(
        task["id"], revision=task["revision"], run_id="desktop-run", command_id="claim"
    )


def test_handoff_roundtrip_has_ordered_progress_and_separate_delivery(hosts):
    task = claim(hosts, submit(hosts))
    task = hosts["desktop"].update(
        task["id"],
        revision=task["revision"],
        run_id="desktop-run",
        command_id="progress",
        state="running",
        summary="Writing the note",
    )
    task = hosts["desktop"].update(
        task["id"],
        revision=task["revision"],
        run_id="desktop-run",
        command_id="finish",
        state="completed",
        summary="The note is ready",
        evidence=["Read-back matched the expected text"],
    )
    assert task["state"] == "completed"
    assert hosts["mobile"].work_locks() == [task["id"]]
    assert [e["seq"] for e in hosts["mobile"].events(task["id"])] == [1, 2, 3, 4]
    out = hosts["transport"].outbox()
    assert [o["state"] for o in out] == ["pending", "pending"]
    sent = hosts["transport"].delivery(out[1]["id"], command_id="send", state="sending")
    accepted = hosts["transport"].delivery(
        sent["id"], command_id="receipt", state="accepted", message_id="platform-id"
    )
    assert accepted["read_at"] is None
    assert accepted["message_id"] == "platform-id"
    assert hosts["mobile"].outbox(include_accepted=True)[1]["state"] == "accepted"
    assert hosts["mobile"].work_locks() == []


def test_duplicate_command_returns_receipt_but_changed_payload_conflicts(hosts):
    task = submit(hosts)
    assert submit(hosts) == task
    claimed = claim(hosts, task)
    assert claim(hosts, task) == claimed
    with pytest.raises(HandoffConflict):
        hosts["desktop"].claim(
            task["id"], revision=1, run_id="different-run", command_id="claim"
        )
    with pytest.raises(HandoffConflict):
        hosts["desktop"].update(
            task["id"],
            revision=1,
            run_id="desktop-run",
            command_id="stale",
            state="running",
            summary="Stale change",
        )


def test_concurrent_claims_have_one_executor(hosts):
    task = submit(hosts)

    def attempt(i):
        try:
            return hosts["desktop"].claim(
                task["id"], revision=1, run_id=f"run-{i}", command_id=f"claim-{i}"
            )
        except HandoffConflict:
            return None

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(attempt, range(2)))
    assert sum(r is not None for r in results) == 1


def test_restart_preserves_lock_and_cancellation_is_an_executor_request(hosts):
    task = claim(hosts, submit(hosts))
    task = hosts["mobile"].cancel(
        task["id"],
        revision=task["revision"],
        command_id="stop",
        reason="Owner stopped the task",
    )
    desktop = hosts["desktop"]
    restored = TaskHandoffs(
        desktop.file, __import__("json").loads(desktop.scope), "desktop"
    )
    assert restored.get(task["id"])["state"] == "accepted"
    assert restored.get(task["id"])["cancel_requested"]
    with pytest.raises(HandoffConflict):
        restored.update(
            task["id"],
            revision=task["revision"],
            run_id="desktop-run",
            command_id="ignore-stop",
            state="completed",
            summary="Should not continue",
            evidence=["old work"],
        )
    with pytest.raises(HandoffConflict):
        restored.claim(
            task["id"],
            revision=task["revision"],
            run_id="replacement",
            command_id="reclaim",
        )
    stopped = restored.update(
        task["id"],
        revision=task["revision"],
        run_id="desktop-run",
        command_id="stopped",
        state="canceled",
        summary="Executor stopped",
    )
    assert stopped["state"] == "canceled"


def test_uncertain_delivery_cannot_be_replayed_with_a_new_command(hosts):
    task = claim(hosts, submit(hosts))
    hosts["desktop"].update(
        task["id"],
        revision=task["revision"],
        run_id="desktop-run",
        command_id="progress",
        state="running",
        summary="Progress",
    )
    host = hosts["transport"]
    out = host.outbox()[0]
    host.delivery(out["id"], command_id="send", state="sending")
    host.delivery(out["id"], command_id="timeout", state="unconfirmed")
    with pytest.raises(HandoffConflict):
        host.delivery(out["id"], command_id="retry-new-id", state="sending")
    with pytest.raises(ValueError):
        host.delivery(out["id"], command_id="no-id", state="accepted")
    reconciled = host.delivery(
        out["id"],
        command_id="lookup-original",
        state="accepted",
        message_id="original-platform-id",
    )
    assert reconciled["message_id"] == "original-platform-id"


def test_sender_cannot_forge_execution_or_delivery_and_scopes_are_isolated(hosts):
    task = submit(hosts)
    with pytest.raises(HandoffConflict):
        hosts["mobile"].claim(task["id"], revision=1, run_id="self", command_id="forge")
    with pytest.raises(ValueError):
        hosts["desktop"].delivery(
            "outbox", command_id="forge", state="accepted", message_id="pretend"
        )
    other = TaskHandoffs(hosts["desktop"].file, {"project": "different"}, "desktop")
    with pytest.raises(HandoffConflict):
        other.get(task["id"])


def test_completion_requires_evidence_and_unclaimed_cancel_never_starts(hosts):
    task = claim(hosts, submit(hosts))
    with pytest.raises(ValueError):
        hosts["desktop"].update(
            task["id"],
            revision=2,
            run_id="desktop-run",
            command_id="finish",
            state="completed",
            summary="Done",
        )
    other = submit(hosts, "new-request")
    other = hosts["mobile"].cancel(
        other["id"], revision=1, command_id="cancel-new", reason="No longer needed"
    )
    assert other["state"] == "canceled"
    with pytest.raises(HandoffConflict):
        hosts["desktop"].claim(
            other["id"], revision=2, run_id="late", command_id="late"
        )
