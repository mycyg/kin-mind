"""Model lanes (stage 2 WP5): one admission ledger for Python and the Node adapters.

Foreground is always admitted, user work keeps a reserved slot that does not yield, capacity
is never a silent default, a lost lease quarantines the late result, and a thread started
on a caller's behalf reuses that caller's lease. Clocks are injected, threads meet on events
and barriers, and nothing reaches a network."""

import copy
import json
import sqlite3
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from test_appraisal_retry_policy import metrics, requeue, saved
from test_autonomous_plans import create, decide, env  # noqa: F401 - `env` is a fixture
from test_operational_recovery import Provider

from eventmem.core.api import create_app
from eventmem.core.db import Conflict
from kin_mind import model_lanes
from kin_mind.adaptive_recall import bounded
from kin_mind.appraisal import Appraisals
from kin_mind.conflicts import classify
from kin_mind.context import CompressedEntry, Compression, Contexts
from kin_mind.lifecycle import foreground_lease
from kin_mind.model_lanes import Ledger, declared, evaluation_slot
from kin_mind.model_runtime import ModelAdmissionWait, background_calls, configure_capacity, model_slot
from kin_mind.operational_status import operational_status

pytest_plugins = ("test_memory_continuity",)


def held(engine):
    """{lane: rows} as any process would count them."""
    with engine.db.connect() as conn:
        return dict(conn.execute("SELECT lane,COUNT(*) FROM mind_model_leases GROUP BY lane").fetchall())


def background(engine):
    return SimpleNamespace(engine=engine, background=True)


def undeclared(engine):
    return SimpleNamespace(engine=engine, background=False)


@pytest.fixture
def now(monkeypatch):
    """The ledger's injected clock."""
    value = [1_800_000_000.0]
    monkeypatch.setattr(model_lanes, "clock", lambda: value[0])
    return value


# --- the three pools ---------------------------------------------------------------------


def test_foreground_is_admitted_while_background_is_full(system):
    mind, *_ = system
    configure_capacity(mind.engine, 2)
    ledger = Ledger(mind.engine.db.path)
    assert [ledger.acquire("background", "fixture-job")["state"] for _ in range(2)] == ["admitted", "admitted"]
    full = ledger.acquire("background", "fixture-job")
    assert (full["state"], full["reason"]) == ("wait", "deepseek-background-capacity")
    answer = ledger.acquire("foreground", "classify", holder="node:fixture")
    assert answer["state"] == "admitted" and answer["capacity"]["limit"] is None
    # The Python side of the same ledger: a declared foreground call is admitted and recorded.
    with declared("foreground", "share-preflight"):
        with model_slot(undeclared(mind.engine), "review_public_reply") as lease:
            assert lease.lane == "foreground" and held(mind.engine) == {"background": 2, "foreground": 2}
    assert held(mind.engine) == {"background": 2, "foreground": 1}
    with pytest.raises(ModelAdmissionWait, match="^deepseek-background-capacity$"):
        with model_slot(background(mind.engine), "submit_event_digest"):
            pass


def test_user_work_is_reserved_and_does_not_yield_to_a_held_foreground_lease(system):
    """The deadlock: a held work task keeps a foreground lease, every background caller waits
    for it, and the review that would release the task must still be admitted."""
    mind, *_ = system
    configure_capacity(mind.engine, 1)
    ledger = Ledger(mind.engine.db.path)
    assert ledger.acquire("background", "fixture-job")["state"] == "admitted"
    foreground_lease(mind.engine, mind.scope.key(), "host:fixture-session", seconds=120)
    assert ledger.acquire("background", "audit")["reason"] == "deepseek-background-capacity"
    configure_capacity(mind.engine, 2)
    assert ledger.acquire("background", "audit")["reason"] == "deepseek-foreground-priority"
    review = ledger.acquire("user-work", "review-work", holder="node:fixture")
    assert review["state"] == "admitted"
    assert review["capacity"] == {"limit": 1, "source": "reserved", "held": 1, "yields_to_foreground": False}
    # One reserved slot: it is neither starved by background work nor unbounded.
    second = ledger.acquire("user-work", "review-work")
    assert (second["state"], second["reason"]) == ("wait", "deepseek-user-work-capacity")
    assert ledger.release(review["lease"]["id"])["state"] == "released"
    with declared("user-work", "review-work"):
        with model_slot(undeclared(mind.engine), "review_work_lock") as lease:
            assert lease.lane == "user-work"
    assert held(mind.engine) == {"background": 1}


def test_concurrent_admissions_never_exceed_the_capacity(system):
    from concurrent.futures import ThreadPoolExecutor
    mind, *_ = system
    configure_capacity(mind.engine, 3)
    ledger = Ledger(mind.engine.db.path, busy_ms=30000)
    start, lock, active, peak, admitted = threading.Barrier(8), threading.Lock(), [0], [0], [0]

    def worker(_):
        start.wait(10)
        for _ in range(6):
            answer = ledger.acquire("background", "fixture-job")
            if answer["state"] != "admitted":
                assert answer["reason"] == "deepseek-background-capacity"
                continue
            with lock:
                active[0] += 1
                admitted[0] += 1
                peak[0] = max(peak[0], active[0])
            with lock:  # Counted down before the row goes, so this never exceeds what the ledger holds.
                active[0] -= 1
            assert ledger.release(answer["lease"]["id"])["state"] == "released"
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(worker, range(8)))
    assert 1 <= peak[0] <= 3 and admitted[0] >= 3 and held(mind.engine) == {}


def test_the_two_wait_strings_other_modules_match_on_are_unchanged(system):
    import inspect

    from eventmem.core import jobs
    from kin_mind import recovery
    assert (model_lanes.WAIT_CAPACITY, model_lanes.WAIT_FOREGROUND) == ("deepseek-background-capacity", "deepseek-foreground-priority")
    for module in (jobs, recovery):
        assert '{"deepseek-background-capacity", "deepseek-foreground-priority"}' in inspect.getsource(module)
    mind, *_ = system
    foreground_lease(mind.engine, mind.scope.key(), "host:fixture-session")
    with pytest.raises(ModelAdmissionWait) as waiting:
        with model_slot(background(mind.engine), "submit_event_digest"):
            pass
    assert str(waiting.value) == "deepseek-foreground-priority"


# --- capacity ----------------------------------------------------------------------------


def test_missing_capacity_is_reported_never_a_silent_default(system):
    mind, *_ = system
    Appraisals(mind)  # The queue the status reads; the host always has one.
    ledger = Ledger(mind.engine.db.path)
    answer = ledger.acquire("background", "fixture-job")
    assert answer["capacity"] == {"limit": 2, "source": "default-unconfigured", "held": 1}
    assert metrics(mind, "model_capacity_unconfigured") == [{"limit": 2, "source": "default-unconfigured"}]
    assert operational_status(mind)["model_lanes"]["lanes"]["background"]["source"] == "default-unconfigured"
    assert configure_capacity(mind.engine, 4)["background_model_limit"] == 4
    assert ledger.acquire("background", "fixture-job")["capacity"] == {"limit": 4, "source": "configured", "held": 2}
    assert len(metrics(mind, "model_capacity_unconfigured")) == 1
    assert operational_status(mind)["model_lanes"]["lanes"]["background"] == {"limit": 4, "source": "configured", "held": 2}
    # Foreground and user work never depend on that limit, so they do not warn about it.
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("DELETE FROM meta WHERE key='kin_background_model_limit'")
    ledger.acquire("foreground", "classify")
    ledger.acquire("user-work", "review-work")
    assert len(metrics(mind, "model_capacity_unconfigured")) == 1


def test_the_host_pushes_its_configured_capacity_into_meta(system):
    from kin_mind.host import dispatch
    mind, *_ = system

    def limit():
        with mind.engine.db.connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key='kin_background_model_limit'").fetchone()
        return row[0] if row else None
    config = {"root": str(mind.engine.db.root), "scope": mind.scope.model_dump(), "agent_version": "fixture-v1", "session_id": "fixture"}
    dispatch(config, "operational-status", {})
    assert limit() is None  # A host that states no capacity configures none.
    config["background_model_limit"] = 4
    status = dispatch(config, "operational-status", {})
    assert limit() == 4 and status["model_lanes"]["lanes"]["background"]["source"] == "configured"
    # An operator's change holds between restarts; the host's start-up call restores the file.
    assert dispatch(config, "configure-model-capacity", {"limit": 3})["background_model_limit"] == 3
    dispatch(config, "operational-status", {})
    assert limit() == 3
    dispatch(config, "recover", {})
    assert limit() == 4
    # A value outside 1..8 configures nothing and is not silent either.
    dispatch({**config, "background_model_limit": 40}, "recover", {})
    assert limit() == 4 and metrics(mind, "model_capacity_invalid") == [{"source": "host-config"}]


# --- renewal, loss and the late result ---------------------------------------------------


def stall(engine, now):
    """The holder stops for longer than TTL, and somebody else's admission sweeps what expired."""
    now[0] += model_lanes.TTL + 1
    assert Ledger(engine.db.path).acquire("foreground", "classify")["state"] == "admitted"


def test_a_failed_renewal_quarantines_the_late_result_and_accounts_its_usage(system, now):
    mind, *_ = system
    late = []
    with pytest.raises(Conflict) as refused:
        with model_slot(background(mind.engine), "submit_event_digest") as lease:
            assert lease.renew() is True
            stall(mind.engine, now)
            assert lease.renew() is False and lease.lost
            late.append("a result that arrived after the lease was gone")
    found = classify(refused.value)
    assert (found.kind, found.code, found.handling) == ("runtime", "model-lease-lost", "block")
    assert refused.value.target == lease.id and held(mind.engine) == {"foreground": 1}
    assert metrics(mind, "model_lease_lost") == [{"lease": lease.id[:8], "lane": "background", "purpose": "submit_event_digest",
                                                  "reason": "renewal-found-no-row", "usage_status": "unknown"}]
    # A caller that had already read the provider's usage leaves it with the lease. No renewal
    # noticed this loss: returning a row that is no longer there is the second check.
    with pytest.raises(Conflict):
        with model_slot(background(mind.engine), "replay_procedure") as lease:
            stall(mind.engine, now)
            lease.usage = {"input_tokens": 12, "output_tokens": 3}
    assert metrics(mind, "model_lease_lost")[1] == {"lease": lease.id[:8], "lane": "background", "purpose": "replay_procedure",
        "reason": "row-missing-at-release", "usage": {"input_tokens": 12, "output_tokens": 3}, "usage_status": "reported"}
    # A failure already on its way out is not replaced, and is accounted all the same.
    with pytest.raises(RuntimeError, match="deepseek-timeout"):
        with model_slot(background(mind.engine), "submit_coverage") as lease:
            stall(mind.engine, now)
            raise RuntimeError("deepseek-timeout")
    assert [m["purpose"] for m in metrics(mind, "model_lease_lost")] == ["submit_event_digest", "replay_procedure", "submit_coverage"]
    # Foreground rows only record: losing one costs nobody their answer.
    with declared("foreground", "memory-context"):
        with model_slot(undeclared(mind.engine), "submit_compression") as lease:
            stall(mind.engine, now)
    assert lease.lost and len(metrics(mind, "model_lease_lost")) == 3


def test_a_late_renewal_keeps_a_row_nobody_swept(system, now):
    """Every admission sweeps before it counts, so a row that is still there was given to nobody else."""
    mind, *_ = system
    configure_capacity(mind.engine, 1)
    with model_slot(background(mind.engine), "submit_event_digest") as lease:
        now[0] += model_lanes.TTL + 30  # The machine slept; no other caller asked meanwhile.
        assert lease.renew() is True and not lease.lost
        assert Ledger(mind.engine.db.path).acquire("background", "fixture-job")["reason"] == "deepseek-background-capacity"
    assert held(mind.engine) == {} and metrics(mind, "model_lease_lost") == []


def test_a_renewal_that_cannot_reach_the_ledger_is_tried_again(system, monkeypatch):
    mind, *_ = system
    with model_slot(background(mind.engine), "submit_event_digest") as lease:
        monkeypatch.setattr(lease.ledger, "renew", lambda *a, **k: {"state": "busy", "reason": "ledger-busy"})
        assert lease.renew() is True and not lease.lost
    assert held(mind.engine) == {} and metrics(mind, "model_lease_lost") == []


class Stalled(Provider):
    """The provider answers, but only after every lease of this worker has expired and been swept."""

    def appraise(self, context):
        with self.engine.db.connect(write=True) as conn:
            conn.execute("DELETE FROM mind_model_leases")
        proposal, receipt = super().appraise(context)
        return proposal, {**receipt, "usage": {"input_tokens": 40, "output_tokens": 9}}


def test_an_evaluation_that_outlives_its_lease_keeps_the_proposal_and_never_commits_it(system):
    mind, memory, source, _ = system
    jobs = Appraisals(mind)
    job = jobs.enqueue([source("current")], "fixture-v1")
    before = mind.read()["revision"]
    provider = Stalled()
    provider.engine = mind.engine
    result = jobs.run_one(provider, lane="action")
    assert (result["state"], result["attempts"]) == ("pending", 1)
    data = saved(mind, job["id"])
    assert data["error_detail"]["code"] == "model-lease-lost" and data["error_detail"]["kind"] == "runtime"
    # Quarantined, not dropped: the judgment and what it cost stay on the row for audit.
    assert data["proposed_result"]["reason"] == "Synthetic current decision"
    assert data["receipt"]["usage"] == {"input_tokens": 40, "output_tokens": 9}
    assert mind.read()["revision"] == before and "result" not in data
    with mind.engine.db.connect() as conn:
        assert not conn.execute("SELECT 1 FROM commands WHERE id=?", (mind._key(job["id"]),)).fetchone()
    lost = metrics(mind, "model_lease_lost")
    assert [(m["purpose"], m["reason"], m["usage_status"]) for m in lost] == [("appraisal:" + job["id"], "row-missing-at-commit", "kept-with-attempt")]
    requeue(mind, job["id"])
    current = Provider()
    current.engine = mind.engine
    assert Appraisals(mind).run_one(current, lane="action")["state"] == "complete"
    assert held(mind.engine) == {}


def test_expired_rows_are_swept_at_every_admission_in_both_tables(system, now):
    mind, *_ = system
    with mind.engine.db.connect(write=True) as conn:
        for index, lane in enumerate(("background", "foreground", "user-work")):
            conn.execute("INSERT INTO mind_model_leases VALUES(?,?,?,?)", ("crashed-%d" % index, lane, now[0] - 1, "{}"))
        conn.execute("INSERT INTO mind_foreground_leases VALUES(?,?,?)", (mind.scope.key(), "read:fixture-old", now[0] - 1))
        conn.execute("INSERT INTO mind_foreground_leases VALUES(?,?,?)", (mind.scope.key(), "host:fixture-live", now[0] + 60))
    assert Ledger(mind.engine.db.path).acquire("foreground", "classify")["state"] == "admitted"
    with mind.engine.db.connect() as conn:
        assert [r[0] for r in conn.execute("SELECT session FROM mind_foreground_leases")] == ["host:fixture-live"]
    assert held(mind.engine) == {"foreground": 1}


def test_writing_a_foreground_lease_sweeps_the_read_leases_nobody_returns(system):
    mind, *_ = system
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("INSERT INTO mind_foreground_leases VALUES(?,?,?)", (mind.scope.key(), "read:fixture-old", time.time() - 1))
    foreground_lease(mind.engine, mind.scope.key(), "read:fixture-new")
    with mind.engine.db.connect() as conn:
        assert [r[0] for r in conn.execute("SELECT session FROM mind_foreground_leases")] == ["read:fixture-new"]


# --- threads -----------------------------------------------------------------------------


def test_a_bounded_thread_reuses_the_parent_lease_instead_of_a_second_slot(system):
    mind, *_ = system
    configure_capacity(mind.engine, 1)  # One slot: a second admission could only wait.
    provider, seen = background(mind.engine), {}

    def rerank():
        with model_slot(copy.copy(provider), "submit_recall_ranking") as lease:
            seen.update(lease=lease, rows=held(mind.engine), thread=threading.current_thread().name)
        return "ranked"
    with model_slot(provider, "appraisal:fixture") as parent:
        assert bounded(rerank, 5) == "ranked"
        assert held(mind.engine) == {"background": 1}
    assert seen == {"lease": parent, "rows": {"background": 1}, "thread": "kin-optional-recall"}
    assert held(mind.engine) == {}


def test_a_rerank_inside_an_evaluation_runs_under_the_evaluations_lease(system, monkeypatch):
    """End to end through collect(): the rerank thread used to take a second background slot, or fail admission."""
    from eventmem.core.providers import Providers
    from kin_mind.adaptive_recall import AdaptiveRecall
    from kin_mind.appraisal import DeepSeek
    mind, memory, source, _ = system
    memory.configure({"graph": True, "graph_recall": True, "adaptive_recall": True})
    source("rank", "A synthetic report about the star map is still unsent")
    configure_capacity(mind.engine, 1)
    monkeypatch.setenv("SYNTHETIC_LANE_KEY", "synthetic")
    monkeypatch.setattr(Providers, "embed", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("unavailable")))
    seen = []

    def respond(request):
        seen.append((threading.current_thread().name, held(mind.engine)))
        allowed = json.loads(json.loads(request.content)["messages"][0]["content"])["allowed_ids"]
        return httpx.Response(200, json={"model": "deepseek-flash", "id": "synthetic-receipt", "stop_reason": "tool_use", "content": [
            {"type": "tool_use", "name": "submit_recall_ranking", "input": {"ids": allowed[:1]}}]})
    provider = DeepSeek("https://api.deepseek.com/anthropic", "deepseek-flash", "SYNTHETIC_LANE_KEY", transport=httpx.MockTransport(respond))
    provider.engine, provider.background = mind.engine, True
    with model_slot(provider, "appraisal:fixture"):
        items, info = AdaptiveRecall(Contexts(mind)).collect("star map report", mode="deep", allow_model=True, provider=provider)
    assert items and seen and all(row == ("kin-optional-recall", {"background": 1}) for row in seen)
    assert not [reason for reason in info["degraded_reasons"] if reason.startswith("rerank")]
    assert held(mind.engine) == {}


def test_a_thread_inherits_the_declared_lane_and_the_job_mark(system):
    mind, *_ = system
    seen = []

    def call():
        with model_slot(undeclared(mind.engine), "submit_recall_ranking") as lease:
            seen.append(lease.lane)
    with declared("foreground", "memory-context"):
        bounded(call, 5)
    with background_calls():
        bounded(call, 5)
        bounded(lambda: bounded(call, 5), 5)  # Two hops, as the embedding call inside the recall executor makes.
    assert seen == ["foreground", "background", "background"]


def test_an_abandoned_late_thread_stops_renewing_and_cannot_keep_its_slot(system, now):
    mind, *_ = system
    configure_capacity(mind.engine, 1)
    entered, leave, finished, seen = threading.Event(), threading.Event(), threading.Event(), {}

    def slow():
        try:
            with model_slot(background(mind.engine), "submit_recall_ranking") as lease:
                seen["lease"] = lease
                entered.set()
                leave.wait(10)
        finally:
            finished.set()
    with pytest.raises(TimeoutError, match="recall-provider-deadline"):
        bounded(slow, 0.2)
    assert entered.wait(5)
    lease = seen["lease"]
    assert lease.abandoned and lease.renew() is False
    ledger = Ledger(mind.engine.db.path)
    assert ledger.acquire("background", "fixture-job")["reason"] == "deepseek-background-capacity"
    now[0] += model_lanes.TTL + 1  # Nothing renews it, so the slot comes back without the thread's help.
    assert ledger.acquire("background", "fixture-job")["state"] == "admitted"
    leave.set()
    assert finished.wait(5)
    assert [m["reason"] for m in metrics(mind, "model_call_abandoned")] == ["caller-deadline"]
    assert metrics(mind, "model_lease_lost") == []


# --- the appraisal row ---------------------------------------------------------------------


def test_the_appraisal_row_lease_is_renewed_during_a_long_call(system, monkeypatch, now):
    mind, memory, source, _ = system
    ticks, count, leases = threading.Condition(), [0], []
    beat = model_lanes.Evaluation.beat

    def observed(self):
        kept = beat(self)
        with ticks:
            count[0] += 1
            ticks.notify_all()
        return kept

    def beats(number):
        with ticks:
            target = count[0] + number
            assert ticks.wait_for(lambda: count[0] >= target, timeout=5)
    monkeypatch.setattr(model_lanes.Evaluation, "beat", observed)
    monkeypatch.setattr(model_lanes, "ROW_BEAT_SECONDS", 0.01)
    job = Appraisals(mind).enqueue([source("current")], "fixture-v1")

    def lease():
        with mind.engine.db.connect() as conn:
            return conn.execute("SELECT lease FROM mind_appraisals WHERE id=?", (job["id"],)).fetchone()[0]

    class Long(Provider):
        def appraise(self, context):
            leases.append(lease())
            now[0] += 1000  # The call outlasts the lease its claim wrote.
            beats(2)        # The second of them certainly read the new clock.
            leases.append(lease())
            now[0] -= 5000
            beats(2)
            leases.append(lease())
            return super().appraise(context)
    assert Appraisals(mind).run_one(Long(), lane="action")["state"] == "complete"
    # Extended while the call runs, and never shortened.
    assert leases[0] < leases[1] == leases[2] == 1_800_001_000.0 + model_lanes.ROW_EXTEND_SECONDS


def test_the_row_heartbeat_stops_once_the_attempt_no_longer_owns_the_row(system):
    mind, memory, source, _ = system
    job = Appraisals(mind).enqueue([source("current")], "fixture-v1")
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET state='running',lease=?,data=json_set(data,'$.attempt_token','mine') WHERE id=?", (time.time() + 60, job["id"]))
    provider = background(mind.engine)
    with evaluation_slot(provider, mind.engine, job["id"], "mine") as guard:
        assert guard.beat() is True and not guard.row_lost
        with mind.engine.db.connect(write=True) as conn:
            conn.execute("UPDATE mind_appraisals SET data=json_set(data,'$.attempt_token','another-worker') WHERE id=?", (job["id"],))
        assert guard.beat() is False and guard.row_lost
        # Nothing more is paid for under a row this attempt no longer owns.
        with pytest.raises(Conflict) as refused:
            with model_slot(provider, "submit_appraisal"):
                pass
        assert (classify(refused.value).code, classify(refused.value).handling) == ("lease-lost", "block")
    assert held(mind.engine) == {}


def test_nothing_more_is_paid_for_once_the_evaluations_lease_is_lost(system, now):
    mind, memory, source, _ = system
    job = Appraisals(mind).enqueue([source("current")], "fixture-v1")
    provider = background(mind.engine)
    with evaluation_slot(provider, mind.engine, job["id"], "token") as guard:
        with model_slot(provider, "submit_compression") as nested:
            assert nested is guard.lease  # Every call of the evaluation runs under its one lease.
        stall(mind.engine, now)
        assert guard.lease.renew() is False
        with pytest.raises(Conflict) as refused:
            with model_slot(provider, "submit_appraisal"):
                pass
        assert classify(refused.value).code == "model-lease-lost"
        with mind.engine.db.connect() as conn, pytest.raises(Conflict, match="Model lease was lost before commit"):
            guard.verify(conn)
    # Leaving the evaluation raises nothing: run_one closes it in a `finally`.
    assert [m["usage_status"] for m in metrics(mind, "model_lease_lost")] == ["kept-with-attempt"]


def test_no_heartbeat_outlives_its_ceiling(monkeypatch):
    """A worker that hangs without dying must lose its row and its slot like one that crashed."""
    beats = []
    monkeypatch.setattr(model_lanes, "MAX_HOLD_SECONDS", -1)
    pulse = model_lanes._Pulse(lambda: beats.append(1) or True, 0.01, "fixture-pulse").start()
    pulse.thread.join(5)
    assert not pulse.thread.is_alive() and beats == []


# --- declarations ------------------------------------------------------------------------


def test_an_undeclared_caller_keeps_the_old_behaviour_and_leaves_a_metric(system):
    mind, *_ = system
    with mind.engine.db.connect(write=True) as conn:
        for index in range(2):
            conn.execute("INSERT INTO mind_model_leases VALUES(?,?,?,?)", (str(index), "background", time.time() + 3600, "{}"))
    with model_slot(undeclared(mind.engine), "review_public_reply") as lease:
        assert lease is None and held(mind.engine) == {"background": 2}
    assert metrics(mind, "model_lane_undeclared") == [{"purpose": "review_public_reply"}]
    # The older marks are declarations of the background lane and need nothing new.
    with pytest.raises(ModelAdmissionWait):
        with model_slot(background(mind.engine), "submit_event_digest"):
            pass
    with background_calls(), pytest.raises(ModelAdmissionWait):
        with model_slot(undeclared(mind.engine), "summary"):
            pass
    assert len(metrics(mind, "model_lane_undeclared")) == 1


def test_a_call_site_may_state_a_default_lane_and_a_job_overrules_it(system):
    mind, *_ = system
    with model_slot(undeclared(mind.engine), "rerank", default="foreground") as lease:
        assert lease.lane == "foreground"
    with background_calls():
        with model_slot(undeclared(mind.engine), "summary", default="foreground") as lease:
            assert lease.lane == "background"
    assert metrics(mind, "model_lane_undeclared") == []


def test_core_recall_is_foreground_and_a_core_job_is_background(system, monkeypatch):
    from eventmem.core.providers import Providers
    mind, *_ = system
    monkeypatch.setenv("SYNTHETIC_ROLE_KEY", "synthetic")
    mind.engine.settings("models", {"query": {"endpoint": "https://api.deepseek.com/v1", "model": "deepseek-synthetic", "api_key_env": "SYNTHETIC_ROLE_KEY"}})
    seen = []

    class Client:
        def __init__(self, **options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *failure):
            return False

        def post(self, url, **request):
            seen.append(held(mind.engine))
            return SimpleNamespace(status_code=200, json=lambda: {"usage": {"prompt_tokens": 3, "completion_tokens": 1}})
    monkeypatch.setattr("eventmem.core.providers.httpx.Client", Client)
    Providers(mind.engine).request("query", "chat/completions", json_={})
    with background_calls():
        Providers(mind.engine).request("query", "chat/completions", json_={})
    assert seen == [{"foreground": 1}, {"background": 1}] and held(mind.engine) == {}


def test_a_context_declares_its_lane_from_what_it_is_for(system, monkeypatch):
    mind, *_ = system
    assert [model_lanes.context_lane(*case) for case in (("chat",), ("work",), ("read",), ("startup",), ("proactive",),
            ("chat", "maintenance"), ("read", "maintenance"))] == ["foreground"] * 4 + ["background"] * 3
    seen = []

    def pack(self, items, query, budget, **options):
        seen.append(model_lanes._declared.get())
        return {"text": "", "tokens": 0, "state": "original", "covered_ids": [], "omitted_ids": [], "items": [], "cache_hit": False, "model_requests": 0}
    monkeypatch.setattr(Contexts, "pack", pack)
    contexts = Contexts(mind)
    contexts.build(query="synthetic question", purpose="chat")
    contexts.build(query="synthetic question", purpose="chat", access_origin="maintenance")
    contexts.build(query="synthetic question", purpose="proactive")
    contexts.read_history("work", query="synthetic question")
    with background_calls():  # A job that recalls stays a job, whatever the helper would have said.
        contexts.build(query="synthetic question", purpose="chat")
    with declared("background", "session-checkpoint"):  # So does a host action that already said what it serves.
        contexts.build(query="synthetic question", purpose="chat")
    assert seen == [("foreground", "memory-context"), ("background", "memory-context"), ("background", "memory-context"),
                    ("foreground", "memory-context"), None, ("background", "session-checkpoint")]
    assert model_lanes._declared.get() is None


def test_host_actions_declare_what_they_serve(system, monkeypatch):
    from kin_mind import host
    from kin_mind.reply_review import ReplyReviews
    from kin_mind.session_checkpoint import SessionCheckpoint
    from kin_mind.sharing import ShareLedger
    mind, *_ = system
    seen = []

    def observe(*args, **kwargs):
        seen.append(model_lanes._declared.get())
        return {"state": "observed"}
    monkeypatch.setattr(host.DeepSeek, "from_engine", classmethod(lambda cls, engine: SimpleNamespace(engine=engine, timeout=600)))
    for owner, name in ((ReplyReviews, "preflight"), (ShareLedger, "preflight"), (SessionCheckpoint, "build")):
        monkeypatch.setattr(owner, name, observe)
    config = {"root": str(mind.engine.db.root), "scope": mind.scope.model_dump(), "agent_version": "fixture-v1", "session_id": "fixture"}
    host.dispatch(config, "share-preflight-group", {"allow_model": True})
    host.dispatch(config, "share-preflight", {"allow_model": True})
    host.dispatch(config, "session-checkpoint", {"snapshot": {}, "binding": {}})
    host.dispatch(config, "session-checkpoint", {"snapshot": {}, "binding": {}, "access_origin": "maintenance"})
    assert seen == [("foreground", "share-preflight-group"), ("foreground", "share-preflight"),
                    ("foreground", "session-checkpoint"), ("background", "session-checkpoint")]


def compressor(mind, monkeypatch, seen):
    from kin_mind.appraisal import DeepSeek
    monkeypatch.setenv("SYNTHETIC_LANE_KEY", "synthetic")

    def respond(request):
        seen.append(held(mind.engine))
        return httpx.Response(200, json={"model": "deepseek-flash", "id": "synthetic-receipt", "stop_reason": "tool_use", "content": [
            {"type": "tool_use", "name": "submit_compression", "input": Compression(entries=[CompressedEntry(item_ids=["long"], summary="The file was made; sending failed.")]).model_dump()}]})
    provider = DeepSeek("https://api.deepseek.com/anthropic", "deepseek-flash", "SYNTHETIC_LANE_KEY", transport=httpx.MockTransport(respond))
    provider.engine = mind.engine
    return provider


def test_the_same_compression_is_foreground_for_a_reader_and_background_for_maintenance(system, monkeypatch):
    mind, *_ = system
    seen, items = [], [{"id": "long", "text": "The file was made; sending failed. " * 100}]
    provider = compressor(mind, monkeypatch, seen)
    with mind.engine.db.connect(write=True) as conn:
        for index in range(2):  # Background is full.
            conn.execute("INSERT INTO mind_model_leases VALUES(?,?,?,?)", (str(index), "background", time.time() + 3600, "{}"))
    with declared(model_lanes.context_lane("chat"), "memory-context"):
        assert Contexts(mind).pack(items, "what happened", 200, provider=provider)["state"] == "compressed"
    assert seen == [{"background": 2, "foreground": 1}]
    with declared(model_lanes.context_lane("chat", "maintenance"), "memory-context"):
        waiting = Contexts(mind).pack(items, "what happened to it", 200, provider=provider)
    assert (waiting["state"], waiting["reason"]) == ("needs-compression", "deepseek-background-capacity") and len(seen) == 1


def test_plan_claims_yield_to_a_foreground_session_of_any_scope(env):  # noqa: F811
    mind, plans, source, clock, initial = env
    decide(env, create(env))
    foreground_lease(mind.engine, '{"another":"scope"}', "host:fixture-session", seconds=120)
    assert plans.claim("create", "worker") == {"state": "waiting", "reason": "user-work-priority"}
    foreground_lease(mind.engine, '{"another":"scope"}', "host:fixture-session", active=False)
    assert plans.claim("create", "worker")["state"] == "claimed"


# --- the switch --------------------------------------------------------------------------


def test_with_the_flag_off_admission_is_what_it_was(system, monkeypatch):
    mind, memory, source, _ = system
    memory.configure({"model_lanes": False})
    ledger = Ledger(mind.engine.db.path)
    assert ledger.acquire("user-work", "review-work") == {"state": "disabled"}
    # Declared and undeclared foreground callers take no lease and leave no metric.
    with declared("foreground", "memory-context"):
        with model_slot(undeclared(mind.engine), "submit_compression") as lease:
            assert lease is None and held(mind.engine) == {}
    # Background: the silent default of two, no quarantine, and a thread that starts with nothing.
    seen = []

    def rerank():
        try:
            with model_slot(background(mind.engine), "submit_recall_ranking"):
                seen.append(held(mind.engine))
        except ModelAdmissionWait as waiting:
            seen.append(str(waiting))
    with model_slot(background(mind.engine), "appraisal:fixture") as lease:
        assert lease is None
        bounded(rerank, 5)
        with mind.engine.db.connect(write=True) as conn:
            conn.execute("INSERT INTO mind_model_leases VALUES(?,?,?,?)", ("another-worker", "background", time.time() + 3600, "{}"))
        bounded(rerank, 5)
        with mind.engine.db.connect(write=True) as conn:
            conn.execute("DELETE FROM mind_model_leases")
    assert seen == [{"background": 2}, "deepseek-background-capacity"]
    for name in ("model_capacity_unconfigured", "model_lane_undeclared", "model_lease_lost"):
        assert metrics(mind, name) == []
    # No row heartbeat and no commit gate either.
    job = Appraisals(mind).enqueue([source("current")], "fixture-v1")
    with evaluation_slot(background(mind.engine), mind.engine, job["id"], "token") as guard:
        assert guard.pulse is None and guard.lease is None
        with mind.engine.db.connect() as conn:
            guard.verify(conn)
    stalled = Stalled()
    stalled.engine = mind.engine
    assert Appraisals(mind).run_one(stalled, lane="action")["state"] == "complete"


def test_the_previous_code_still_works_on_a_ledger_that_lanes_have_used(system):
    """Rollback: positional inserts need the old column count, and the old admission counts
    background rows only, so the rows of the two new lanes are invisible to it."""
    mind, memory, *_ = system
    with mind.engine.db.connect() as conn:
        assert [r[1] for r in conn.execute("PRAGMA table_info(mind_model_leases)")] == ["id", "lane", "expires_at", "data"]
        assert [r[1] for r in conn.execute("PRAGMA table_info(mind_foreground_leases)")] == ["scope", "session", "expires_at"]
    ledger = Ledger(mind.engine.db.path)
    for lane, purpose in (("foreground", "classify"), ("foreground", "chat-turn"), ("foreground", "memory-context"), ("user-work", "review-work")):
        assert ledger.acquire(lane, purpose, holder="node:fixture")["state"] == "admitted"
    memory.configure({"model_lanes": False})  # What a rolled-back worker runs.

    def another_worker():
        with model_slot(background(mind.engine), "submit_event_digest"):
            return held(mind.engine)
    with model_slot(background(mind.engine), "appraisal:fixture"):
        # Two by default, as before, however many rows the new lanes hold.
        assert bounded(another_worker, 5) == {"background": 2, "foreground": 3, "user-work": 1}
        with mind.engine.db.connect(write=True) as conn:
            conn.execute("INSERT INTO mind_model_leases VALUES(?,?,?,?)", ("a-third-worker", "background", time.time() + 3600, "{}"))
        with pytest.raises(ModelAdmissionWait, match="deepseek-background-capacity"):
            bounded(another_worker, 5)


def test_with_the_flag_off_a_plan_claim_looks_at_its_own_scope_only(env):  # noqa: F811
    from kin_mind.memory import MemoryContinuity
    mind, plans, source, clock, initial = env
    decide(env, create(env))
    MemoryContinuity(mind).configure({"model_lanes": False})
    foreground_lease(mind.engine, '{"another":"scope"}', "host:fixture-session", seconds=120)
    assert plans.claim("create", "worker")["state"] == "claimed"


# --- the interface for Node: routes and the CLI fallback -------------------------------------


@pytest.fixture
def client(system):
    mind, *_ = system
    with TestClient(create_app(engine=mind.engine, token="local-test", workers=False, mcp_enabled=False)) as http:
        http.headers["Authorization"] = "Bearer local-test"
        yield http


def test_lease_routes_acquire_renew_release_and_expire(system, client, now):
    mind, *_ = system
    taken = client.post("/v1/model-leases/acquire", json={"lane": "user-work", "purpose": "review-work", "holder": "node:fixture"})
    assert taken.status_code == 200 and taken.json()["state"] == "admitted"
    lease = taken.json()["lease"]
    assert (lease["lane"], lease["ttl_seconds"], lease["renew_after_seconds"], lease["expires_at"]) == ("user-work", 90, 30, now[0] + 90)
    assert client.post("/v1/model-leases/acquire", json={"lane": "user-work", "purpose": "review-work"}).json() == {
        "state": "wait", "reason": "deepseek-user-work-capacity", "retry_after_seconds": 30,
        "capacity": {"limit": 1, "source": "reserved", "held": 1, "yields_to_foreground": False}}
    now[0] += 60
    renewed = client.post("/v1/model-leases/renew", json={"id": lease["id"]}).json()
    assert renewed == {"state": "renewed", "lease": {"id": lease["id"], "expires_at": now[0] + 90, "ttl_seconds": 90, "renew_after_seconds": 30}}
    status = client.get("/v1/model-leases").json()
    assert status["holders"] == [{"lease": lease["id"][:8], "lane": "user-work", "purpose": "review-work", "holder": "node:fixture",
                                  "held_seconds": 60, "expires_in_seconds": 90}]
    assert client.post("/v1/model-leases/release", json={"id": lease["id"]}).json() == {"state": "released", "id": lease["id"]}
    assert client.post("/v1/model-leases/release", json={"id": lease["id"]}).json() == {"state": "lost", "id": lease["id"]}
    # Expiry is the crash recovery: after TTL the slot goes to the next caller, and the one
    # that stopped renewing learns that its lease is lost.
    crashed = client.post("/v1/model-leases/acquire", json={"lane": "user-work", "purpose": "review-work"}).json()["lease"]
    now[0] += 89
    assert client.post("/v1/model-leases/acquire", json={"lane": "user-work", "purpose": "review-work"}).json()["state"] == "wait"
    now[0] += 2
    assert client.get("/v1/model-leases").json()["lanes"]["user-work"]["held"] == 0
    successor = client.post("/v1/model-leases/acquire", json={"lane": "user-work", "purpose": "review-work"}).json()
    assert successor["state"] == "admitted"
    assert client.post("/v1/model-leases/renew", json={"id": crashed["id"]}).json() == {"state": "lost", "id": crashed["id"]}
    assert client.post("/v1/model-leases/release", json={"id": crashed["id"]}).json() == {"state": "lost", "id": crashed["id"]}
    assert held(mind.engine) == {"user-work": 1}


def test_a_lease_id_chosen_by_the_caller_makes_acquire_repeatable(client, system):
    mind, *_ = system
    request = {"lane": "user-work", "purpose": "review-work", "id": "node-fixture-0001"}
    first = client.post("/v1/model-leases/acquire", json=request).json()
    # The answer was lost on the way: asking again neither waits on itself nor takes a second slot.
    again = client.post("/v1/model-leases/acquire", json=request).json()
    assert first["state"] == again["state"] == "admitted" and again["lease"]["id"] == "node-fixture-0001"
    assert again["capacity"]["held"] == 1 and held(mind.engine) == {"user-work": 1}
    assert client.post("/v1/model-leases/acquire", json={**request, "lane": "background"}).status_code == 422
    assert client.post("/v1/model-leases/release", json={"id": "node-fixture-0001"}).json()["state"] == "released"


def test_a_busy_ledger_answers_within_the_short_timeout(system, client, monkeypatch):
    mind, *_ = system
    monkeypatch.setattr(model_lanes, "LEDGER_BUSY_MS", 150)
    writer = sqlite3.connect(mind.engine.db.path, isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    try:
        started = time.monotonic()
        answer = client.post("/v1/model-leases/acquire", json={"lane": "foreground", "purpose": "classify"})
        assert time.monotonic() - started < 2
        assert answer.status_code == 503 and answer.json() == {"state": "busy", "reason": "ledger-busy"}
        assert client.post("/v1/model-leases/renew", json={"id": "node-fixture-0001"}).status_code == 503
        # In process the same rule holds: foreground goes ahead unrecorded, and does not ask again at once.
        with declared("foreground", "memory-context"):
            with model_slot(undeclared(mind.engine), "submit_compression") as lease:
                assert lease is None

            def asked_again(*args, **kwargs):
                raise AssertionError("a busy ledger is not asked again at once")
            with monkeypatch.context() as patched:
                patched.setattr(Ledger, "acquire", asked_again)
                with model_slot(undeclared(mind.engine), "submit_compression") as lease:
                    assert lease is None
    finally:
        writer.rollback()
        writer.close()
    assert client.post("/v1/model-leases/acquire", json={"lane": "foreground", "purpose": "classify"}).json()["state"] == "admitted"


def test_lease_routes_are_internal_authenticated_and_take_labels_only(system, client):
    mind, *_ = system
    paths = client.get("/v1/openapi.json").json()["paths"]
    assert not [path for path in paths if "model-lease" in path]
    assert TestClient(client.app).post("/v1/model-leases/acquire", json={"lane": "foreground", "purpose": "classify"}).status_code == 401
    for body in ({"lane": "foreground", "purpose": "a sentence is not a label"}, {"lane": "foreground", "purpose": "合成文本"},
                 {"lane": "urgent", "purpose": "classify"}, {"lane": "foreground", "purpose": "classify", "ttl_seconds": 5},
                 {"lane": "foreground", "purpose": "classify", "holder": "who is this"}):
        assert client.post("/v1/model-leases/acquire", json=body).status_code == 422
    assert held(mind.engine) == {}
    MemoryOff = __import__("kin_mind.memory", fromlist=["MemoryContinuity"]).MemoryContinuity
    MemoryOff(mind).configure({"model_lanes": False})
    assert client.post("/v1/model-leases/acquire", json={"lane": "background", "purpose": "audit"}).json() == {"state": "disabled"}


def test_the_cli_fallback_answers_without_opening_an_engine(system, monkeypatch, now):
    from kin_mind import host
    mind, *_ = system
    config = {"root": str(mind.engine.db.root)}

    def forbidden(*args, **kwargs):
        raise AssertionError("model-lease must not open an engine")
    monkeypatch.setattr(host, "Engine", forbidden)
    taken = host.dispatch(config, "model-lease", {"op": "acquire", "lane": "background", "purpose": "audit", "holder": "node:fixture"})
    assert taken["state"] == "admitted" and taken["capacity"]["source"] == "default-unconfigured"
    assert host.dispatch(config, "model-lease", {"op": "renew", "id": taken["lease"]["id"]})["state"] == "renewed"
    assert host.dispatch(config, "model-lease", {"op": "status"})["lanes"]["background"]["held"] == 1
    assert host.dispatch(config, "model-lease", {"op": "release", "id": taken["lease"]["id"]})["state"] == "released"
    assert host.dispatch(config, "model-lease", {"op": "renew", "id": taken["lease"]["id"]})["state"] == "lost"
    with pytest.raises(ValueError, match="Unknown lease operation"):
        host.dispatch(config, "model-lease", {"op": "steal"})
    monkeypatch.setattr(model_lanes, "LEDGER_BUSY_MS", 150)
    writer = sqlite3.connect(mind.engine.db.path, isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    try:
        assert host.dispatch(config, "model-lease", {"op": "acquire", "lane": "foreground", "purpose": "classify"}) == {"state": "busy", "reason": "ledger-busy"}
    finally:
        writer.rollback()
        writer.close()
    assert host.dispatch({"root": str(mind.engine.db.root / "absent")}, "model-lease", {"op": "status"}) == {"state": "unavailable", "reason": "ledger-missing"}


def test_operational_status_reports_lanes_capacity_and_holders_without_text(system, now):
    mind, *_ = system
    Appraisals(mind)
    configure_capacity(mind.engine, 4)
    foreground_lease(mind.engine, mind.scope.key(), "host:fixture-session", seconds=10 ** 9)
    with mind.engine.db.connect(write=True) as conn:  # A row written by code that predates lanes.
        conn.execute("INSERT INTO mind_model_leases VALUES(?,?,?,?)", ("older-code-row", "background", now[0] + 60, json.dumps({"purpose": "含有私密文本的用途"})))
    with declared("foreground", "share-preflight"):
        with model_slot(undeclared(mind.engine), "review_public_reply"):
            status = operational_status(mind)
    lanes = status["model_lanes"]
    assert lanes["enabled"] and lanes["foreground_sessions"] == 1 and lanes["background_yields"]
    assert lanes["lanes"] == {"background": {"limit": 4, "source": "configured", "held": 1},
                              "user-work": {"limit": 1, "source": "reserved", "held": 0, "yields_to_foreground": False},
                              "foreground": {"limit": None, "source": "unbounded", "held": 1}}
    assert [(h["lane"], h["purpose"]) for h in lanes["holders"]] == [("background", "unlabelled"), ("foreground", "review_public_reply")]
    assert lanes["holders"][1]["holder"].startswith("python:") and "私密" not in json.dumps(status, ensure_ascii=False)
    assert status["autonomy"]["background_model_slots"] == 1
