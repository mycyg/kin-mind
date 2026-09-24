"""Work locks of the task exchange (E3-11): a finished hand-off never holds the
phone's work for good, whatever became of its closing notice."""

from eventmem.core.handoffs import TaskHandoffs


def stores(tmp_path):
    db = tmp_path / "task-exchange.sqlite3"
    scope = {"owner": "synthetic"}
    return (
        TaskHandoffs(db, scope, "mobile"),
        TaskHandoffs(db, scope, "desktop"),
        TaskHandoffs(db, scope, "transport"),
    )


def started(mobile, desktop, command):
    task = mobile.submit(
        command_id=command,
        recipient="desktop",
        session_id="session-1",
        title="A task",
        goal="Do the thing",
        source="owner asked",
        acceptance="the thing exists",
    )
    task = desktop.claim(
        task["id"], revision=task["revision"], run_id="run-1", command_id=command + "-claim"
    )
    return task


def finish(desktop, task, state, command, notify=True):
    return desktop.update(
        task["id"],
        revision=task["revision"],
        run_id="run-1",
        command_id=command,
        state=state,
        summary="finished as " + state,
        evidence=["checked"] if state == "completed" else None,
        notify=notify,
    )


def test_a_running_task_holds_and_a_failed_one_releases_once_told(tmp_path):
    mobile, desktop, transport = stores(tmp_path)
    task = started(mobile, desktop, "t1")
    assert mobile.work_locks() == [task["id"]]
    failed = finish(desktop, task, "failed", "t1-fail")
    assert mobile.work_locks() == [task["id"]], "the failure notice is still on its way"
    notice = [o for o in transport.outbox() if o["task_id"] == task["id"] and o["kind"] == "failed"][0]
    transport.delivery(notice["id"], command_id="send-1", state="sending")
    transport.delivery(notice["id"], command_id="sent-1", state="accepted", message_id="m1")
    assert failed["state"] == "failed"
    assert mobile.work_locks() == []


def test_a_notice_that_failed_or_stayed_unconfirmed_never_holds_for_good(tmp_path):
    mobile, desktop, transport = stores(tmp_path)
    for command, outcome in (("a", "failed"), ("b", "unconfirmed")):
        task = started(mobile, desktop, command)
        finish(desktop, task, "completed", command + "-done")
        notice = [o for o in transport.outbox() if o["task_id"] == task["id"] and o["kind"] == "completed"][0]
        transport.delivery(notice["id"], command_id=command + "-sending", state="sending")
        transport.delivery(notice["id"], command_id=command + "-" + outcome, state=outcome)
    assert mobile.work_locks() == []


def test_a_task_finished_without_a_notice_releases(tmp_path):
    mobile, desktop, _ = stores(tmp_path)
    task = started(mobile, desktop, "quiet")
    finish(desktop, task, "completed", "quiet-done", notify=False)
    assert mobile.work_locks() == []
