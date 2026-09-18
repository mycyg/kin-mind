"""Recovery commands must find their own evidence, not take the caller's word.

Nothing here asks the machine the tests run on anything. Every process question
goes through an injected probe, so a recycled pid, a stale pid file and a worker
that is genuinely still running are all constructed, not hoped for.
"""
import json
import time

import pytest

from eventmem.core.db import Conflict, dumps
from kin_mind import liveness
from kin_mind.appraisal import Appraisals
from kin_mind.host import dispatch
from kin_mind.recovery import migrate_operational, recover_batched, recover_history

pytest_plugins = ("test_memory_continuity", "test_autonomous_plans")

ALIVE = "Fri Sep 18 10:37:19 2026"
LATER = "Fri Sep 18 11:04:02 2026"


def probe(table):
    """A process table the test writes itself. An absent pid is a dead pid; a pid
    mapped to None is one the probe could not answer for at all."""
    def seen(pid):
        if pid not in table:
            return {"alive": False, "known": True, "started": None, "command": None}
        entry = table[pid]
        if entry is None:
            return {"alive": True, "known": False, "started": None, "command": None}
        return {"alive": True, "known": True, "started": entry[0], "command": entry[1]}
    return seen


def config_for(mind, **extra):
    return {"root": str(mind.engine.db.root), "scope": mind.scope.model_dump(),
            "agent_version": "fixture-v1", "session_id": "fixture", **extra}


def running_appraisal(mind, source, *, lease):
    job = Appraisals(mind).enqueue([source("held")], "fixture-v1")
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET state='running',lease=? WHERE id=?", (lease, job["id"]))
    return job["id"]


def explorations(mind, rows):
    from kin_mind.exploration import Explorations
    Explorations(mind)
    with mind.engine.db.connect(write=True) as conn:
        for identifier, data in rows:
            conn.execute("INSERT INTO mind_explorations VALUES(?,?,?,?,?)",
                         (identifier, mind.scope.key(), "running", mind.clock(), dumps(data)))


def states(mind):
    with mind.engine.db.connect() as conn:
        return {r["id"]: r["state"] for r in conn.execute("SELECT id,state FROM mind_explorations")}


# --- what a pid can and cannot prove ------------------------------------------------


def test_a_pid_that_cannot_be_asked_about_is_alive():
    # The probe answered nothing: the only safe reading is that the worker runs.
    assert liveness.process_gone(4242, started=ALIVE, probe=probe({4242: None})) is False
    assert liveness.process_gone(4242, started=ALIVE, probe=probe({})) is True


def test_a_recycled_pid_is_recognised_by_its_start_time():
    # Same number, a process that started later: the exploration that recorded it
    # is gone, whatever the number says.
    table = probe({4242: (LATER, "/usr/bin/python -u memory_service.py")})
    assert liveness.process_gone(4242, started=ALIVE, probe=table) is True
    assert liveness.process_gone(4242, started=LATER, probe=table) is False


def test_an_identity_mismatch_is_as_good_as_a_dead_pid():
    table = probe({77: (ALIVE, "/usr/bin/vim notes.txt")})
    assert liveness.process_gone(77, command="service.mjs", probe=table) is True
    assert liveness.process_gone(77, command="vim", probe=table) is False
    # A live pid with nothing to check it against stays alive.
    assert liveness.process_gone(77, probe=table) is False


def test_a_record_without_a_pid_or_past_its_deadline():
    now = 1_000_000.0
    table = probe({4242: (ALIVE, "python")})
    # An older row, written before any of this: it cannot be shown to be dead.
    assert liveness.record_alive(None, now=now, probe=table) is True
    assert liveness.record_alive({}, now=now, probe=table) is True
    fresh = {"pid": 4242, "started": ALIVE, "deadline": now + 60}
    assert liveness.record_alive(fresh, now=now, probe=table) is True
    assert liveness.record_alive({**fresh, "deadline": now - 1}, now=now, probe=table) is False


def test_the_real_probe_answers_about_this_process_and_about_nonsense():
    import os

    # True whether or not a `ps` was found: an unanswerable question means alive.
    assert liveness.probe_process(os.getpid())["alive"] is True
    for nonsense in (0, 1, "", "not-a-pid", None):
        answer = liveness.probe_process(nonsense)
        assert answer == {"alive": False, "known": True, "started": None, "command": None}


# --- the four recovery commands -----------------------------------------------------


def test_operational_migration_refuses_while_an_appraisal_lease_is_fresh(system):
    mind, memory, source, _clock = system
    held = running_appraisal(mind, source, lease=time.time() + 600)
    with pytest.raises(Conflict) as refusal:
        migrate_operational(mind, workers_stopped=True)
    assert refusal.value.code == "worker-lease-fresh" and refusal.value.target == held
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET lease=? WHERE id=?", (time.time() - 1, held))
    assert migrate_operational(mind, workers_stopped=False)["state"] == "migrated"


def test_batched_recovery_refuses_while_an_appraisal_lease_is_fresh(system):
    mind, _memory, source, _clock = system
    held = running_appraisal(mind, source, lease=time.time() + 600)
    with pytest.raises(Conflict) as refusal:
        recover_batched(mind, command_id="wp6-held", workers_stopped=True)
    assert refusal.value.code == "worker-lease-fresh"
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET lease=0 WHERE id=?", (held,))
    assert recover_batched(mind, command_id="wp6-held", workers_stopped=False)["state"] == "recovered"


def test_the_historical_resume_asks_for_no_shutdown_at_all(system):
    mind, _memory, source, _clock = system
    jobs = Appraisals(mind)
    quarantined = jobs.enqueue([source("history")], "fixture-v1", stimulus="memory-backfill")["id"]
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET state='needs-repair' WHERE id=?", (quarantined,))
    # Another lane's worker is mid-evaluation. It holds nothing this command touches.
    running_appraisal(mind, source, lease=time.time() + 600)
    result = recover_history(mind, job_ids=[quarantined], command_id="wp6-history",
                             source="An approved historical repair", workers_stopped=False)
    assert result["resumed"] == [quarantined]


def test_a_plan_execution_under_lease_is_reported_not_interrupted(env):
    from test_autonomous_plans import create, decide
    mind, plans, _source, _clock, _initial = env
    plan = decide(env, create(env))
    run = plans.claim("create", "worker")["run"]
    held = plans.recover(workers_stopped=True)
    assert held["recovered"] == [] and [r["id"] for r in held["still_leased"]] == [run["id"]]
    assert plans.read(plan["id"])["plans"][0]["steps"][0]["state"] == "running"
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_plan_runs SET lease_until=? WHERE id=?", (time.time() - 1, run["id"]))
    assert plans.recover()["recovered"] == [run["id"]]


# --- the exploration a host restart must not interrupt --------------------------------


# Only one exploration runs per scope, so each case is its own row. `alive` says
# whether a host restart must leave it where it is; `overdue` moves its deadline
# into the past.
@pytest.mark.parametrize("alive,overdue,record", [
    (True, False, {"pid": 4242, "started": ALIVE}),     # still running
    (False, False, {"pid": 4243, "started": ALIVE}),    # the number was reused
    (False, False, {"pid": 4244, "started": ALIVE}),    # no such process
    (False, True, {"pid": 4242, "started": ALIVE}),     # overdue, however alive
    (True, False, {"pid": 4242}),                       # a live pid, nothing to compare
    (True, False, {"pid": 4245, "started": ALIVE}),     # the probe could not answer
    (True, False, None),                                # a row written before all this
])
def test_a_host_restart_interrupts_only_what_it_can_prove_is_gone(system, monkeypatch, alive, overdue, record):
    mind, _memory, _source, _clock = system
    data = {"desire_id": "d-fixture"}
    if record is not None:
        data["liveness"] = {**record, "deadline": time.time() + (-1 if overdue else 600)}
    explorations(mind, [("explore_case", data)])
    monkeypatch.setattr(liveness, "probe_process",
                        probe({4242: (ALIVE, "python"), 4243: (LATER, "python"), 4245: None}))
    result = dispatch(config_for(mind), "recover", {})
    assert states(mind)["explore_case"] == ("running" if alive else "interrupted")
    assert result["live_explorations"] == (["explore_case"] if alive else [])
    assert result["interrupted_explorations"] == ([] if alive else ["explore_case"])


def test_an_exploration_records_who_is_running_it(system, monkeypatch):
    import os

    from kin_mind.exploration import Explorations
    mind, _memory, _source, _clock = system
    monkeypatch.setattr(liveness, "probe_process", probe({os.getpid(): (ALIVE, "pytest")}))
    assert liveness.self_record(deadline=1_000.0) == {"pid": os.getpid(), "started": ALIVE, "deadline": 1_000.0}
    # A probe that cannot answer leaves no start time, and such a record can then
    # never be shown to be dead — which is the safe direction.
    monkeypatch.setattr(liveness, "probe_process", probe({os.getpid(): None}))
    unidentified = liveness.self_record(deadline=1_000.0)
    assert unidentified["started"] is None
    # A live pid with no recorded start time cannot be told from a reused one, so
    # it counts as the worker that wrote it.
    assert liveness.record_alive(unidentified, now=1.0, probe=probe({os.getpid(): (LATER, "other")})) is True
    # The record lives in `data`: the table gains no column for it.
    Explorations(mind)
    with mind.engine.db.connect() as conn:
        columns = [r[1] for r in conn.execute("PRAGMA table_info(mind_explorations)")]
    assert columns == ["id", "scope", "state", "created_at", "data"]


# --- the flag off restores the previous code ------------------------------------------


def test_with_the_flag_off_the_boolean_decides_again(system, env, monkeypatch):
    mind, memory, source, _clock = system
    memory.configure({"liveness_checks": False})
    running_appraisal(mind, source, lease=time.time() + 600)
    # The old contract: the caller's assertion is both necessary and sufficient.
    for command in (lambda: migrate_operational(mind, workers_stopped=False),
                    lambda: recover_batched(mind, command_id="wp6-off", workers_stopped=False)):
        with pytest.raises(ValueError, match="termination"):
            command()
    assert recover_batched(mind, command_id="wp6-off", workers_stopped=True)["state"] == "recovered"
    explorations(mind, [("explore_live", {"desire_id": "d-live",
                                          "liveness": {"pid": 4242, "started": ALIVE, "deadline": time.time() + 600}})])
    monkeypatch.setattr(liveness, "probe_process", probe({4242: (ALIVE, "python")}))
    assert dispatch(config_for(mind), "recover", {})["interrupted_explorations"] == ["explore_live"]


def test_with_the_flag_off_a_leased_execution_is_interrupted_again(env):
    from test_autonomous_plans import create, decide
    from kin_mind.memory import MemoryContinuity
    mind, plans, _source, _clock, _initial = env
    MemoryContinuity(mind).configure({"liveness_checks": False})
    decide(env, create(env))
    run = plans.claim("create", "worker")["run"]
    with pytest.raises(Conflict):
        plans.recover()
    assert plans.recover(workers_stopped=True)["recovered"] == [run["id"]]


# --- quiet -----------------------------------------------------------------------------


def quiet_host(tmp_path, *, heartbeat_age=600, state="connected", pids=("4242", "4243")):
    (tmp_path / "bridge.pid").write_text(pids[0])
    (tmp_path / "service.pid").write_text(pids[1])
    (tmp_path / "status.json").write_text(json.dumps({
        "state": state,
        "heartbeatAt": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - heartbeat_age)) + "Z"}))
    return {"liveness": {
        "processes": [{"name": "bridge", "pid_file": str(tmp_path / "bridge.pid"), "command": "service.mjs"},
                      {"name": "memory-service", "pid_file": str(tmp_path / "service.pid"), "command": "memory_service.py"}],
        "status_file": str(tmp_path / "status.json")}}


def reasons(verdict):
    return {check["check"]: check["reason"] for check in verdict["checks"]}


def test_quiet_needs_every_clause(system, tmp_path):
    mind, _memory, source, _clock = system
    config = quiet_host(tmp_path)
    gone = probe({})
    verdict = liveness.quiescence(mind, config, probe=gone)
    assert verdict["quiet"] is True
    assert reasons(verdict) == {"process": "process-gone-or-reused", "host-status": "heartbeat-stale",
                                "leases": "no-fresh-lease", "explorations": "no-live-exploration",
                                "exclusive-probe": "exclusive-lock-available"}
    # A live bridge, recognised by the command it is running.
    running = probe({4242: (ALIVE, "/usr/local/bin/node /somewhere/service.mjs")})
    noisy = liveness.quiescence(mind, config, probe=running)
    assert noisy["quiet"] is False
    assert [c["reason"] for c in noisy["blocking"]] == ["process-running", "not-attempted-while-noisy"]
    # A held lease and a live exploration each speak for themselves.
    running_appraisal(mind, source, lease=time.time() + 600)
    explorations(mind, [("explore_live", {"liveness": {"pid": 4242, "started": ALIVE, "deadline": time.time() + 600}})])
    held = liveness.quiescence(mind, config, probe=running)
    assert reasons(held)["leases"] == "leases-fresh" and reasons(held)["explorations"] == "exploration-running"
    assert [t["table"] for t in next(c for c in held["checks"] if c["check"] == "leases")["tables"]] == ["mind_appraisals"]


def test_missing_evidence_is_never_quiet(system, tmp_path):
    mind, _memory, _source, _clock = system
    gone = probe({})
    assert reasons(liveness.quiescence(mind, {}, probe=gone)) == {
        "processes": "no-pid-files-configured", "host-status": "status-file-not-configured",
        "leases": "no-fresh-lease", "explorations": "no-live-exploration",
        "exclusive-probe": "not-attempted-while-noisy"}
    config = quiet_host(tmp_path, heartbeat_age=1)
    assert reasons(liveness.quiescence(mind, config, probe=gone))["host-status"] == "heartbeat-fresh"
    assert reasons(liveness.quiescence(mind, quiet_host(tmp_path, state="stopped", heartbeat_age=1),
                                       probe=gone))["host-status"] == "host-stopped"
    (tmp_path / "status.json").unlink()
    assert reasons(liveness.quiescence(mind, config, probe=gone))["host-status"] == "status-file-missing"
    # A pid file that is absent, or that holds no pid, claims no process at all.
    (tmp_path / "bridge.pid").unlink()
    (tmp_path / "service.pid").write_text("")
    verdict = liveness.quiescence(mind, config, probe=gone)
    assert [c["reason"] for c in verdict["checks"] if c["check"] == "process"] == ["no-pid-file", "pid-file-holds-no-pid"]


def test_the_exclusive_probe_sees_another_writer(system, tmp_path):
    mind, _memory, _source, _clock = system
    config = quiet_host(tmp_path)
    with mind.engine.db.connect(write=True):
        verdict = liveness.quiescence(mind, config, probe=probe({}))
    assert verdict["quiet"] is False and reasons(verdict)["exclusive-probe"] == "database-busy"
