"""The appraisal attempt ledger and honest usage accounting.

Synthetic replays only: an injected clock, scripted providers and httpx.MockTransport.
No model call, no network. Every assertion about cost is about what the host records,
never about what a provider charged.
"""
import json

import httpx
import pytest

from eventmem.core.db import dumps
from eventmem.core.models import ModelRole
from eventmem.core.providers import Providers
from kin_mind import attempts as ledger
from kin_mind.appraisal import Appraisals, DeepSeek
from kin_mind.host import dispatch

pytest_plugins = ("test_memory_continuity",)

DECISION = {"reason": "Synthetic current decision", "values": {"initiative": 77}, "next_review_minutes": 25}
SESSION = {"id": "snapshot-1", "binding": {"generation": 1}, "lastCompaction": {"id": "compact-1", "completedAt": 100},
           "evidence": [{"id": "observed-1", "at": 200, "text": "SYNTHETIC OBSERVATION TEXT"}],
           "recent": [{"id": "turn-1", "role": "user", "text": "SYNTHETIC OWNER WORDS about the clock"}]}
INVENTED_ADVICE = {"reason": "Review", "session_advice": {"action": "recall", "reason": "Thin context",
                                                          "evidenceIds": ["invented-observation"]}}
USAGE_KEYS = {"usage", "cost", "input_tokens", "output_tokens", "prompt_tokens", "completion_tokens"}
# Accounting that must never reach a prompt: it costs tokens and makes the rendered
# request differ on every attempt, which is what WP4 and WP7 compare.
PROMPT_FORBIDDEN = {"model_receipts", "usage", "compression_receipt", "request_id", "usage_status"}


class Unmetered(dict):
    """A 200 answer the provider sent without reporting any usage at all."""


class Api:
    """The provider's endpoint, scripted: one answer per tool call, every request kept.

    An `int` answer is an HTTP status and an exception instance is raised by the transport.
    Compression answers itself, covering exactly the ids the host allowed, so a batch split
    never has to be predicted here.
    """

    def __init__(self, monkeypatch, responses, engine=None, *, timeout=600):
        monkeypatch.setenv("SYNTHETIC_KEY", "test-only-key")
        self.requests, self.responses = [], list(responses)
        self.provider = DeepSeek("https://api.deepseek.com/anthropic", "deepseek-flash", "SYNTHETIC_KEY",
                                 timeout, transport=httpx.MockTransport(self.respond))
        if engine is not None:
            self.provider.engine = engine

    def respond(self, request):
        body = json.loads(request.content)
        name = body["tools"][0]["name"]
        content = json.loads(body["messages"][0]["content"])
        self.requests.append({"tool": name, "content": content, "system": body["system"]})
        if name == "submit_compression":
            answer = {"entries": [{"item_ids": content["allowed_item_ids"], "summary": "A synthetic batch summary."}],
                      "omitted_ids": []}
        else:
            answer = self.responses.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, int):
            return httpx.Response(answer)
        reply = {"model": "deepseek-flash", "id": name + "-" + str(len(self.requests)), "stop_reason": "tool_use",
                 "content": [{"type": "tool_use", "name": name, "input": answer}]}
        if not isinstance(answer, Unmetered):
            reply["usage"] = {"input_tokens": 11, "output_tokens": len(self.requests)}
        return httpx.Response(200, json=reply)

    @property
    def tools(self):
        return [r["tool"] for r in self.requests]


def rows(mind, job_id=None, limit=50):
    return ledger.read(mind.engine, mind.scope.key(), job_id=job_id, limit=limit)["attempts"]


def saved(mind, job_id):
    with mind.engine.db.connect() as conn:
        return json.loads(conn.execute("SELECT data FROM mind_appraisals WHERE id=?", (job_id,)).fetchone()[0])


def metrics(mind, name=None):
    with mind.engine.db.connect() as conn:
        return [{"name": r["name"], "value": r["value"], **json.loads(r["data"])} for r in
                conn.execute("SELECT name,value,data FROM metrics" + (" WHERE name=?" if name else ""),
                             (name,) if name else ()).fetchall()]


def model_metrics(mind):
    """Every metric that could have stood a cost or a token count in for a missing one.

    A recall round that made no model call genuinely counts zero tokens, so its own
    metric is not evidence either way and is left out here.
    """
    return [m for m in metrics(mind) if m["name"].startswith(("model_", "structured_"))]


def purposes(row):
    return [c["purpose"] for c in row["calls"]]


def zeros_written_for_missing_usage(value):
    """Every place a count the provider never reported could have been stored as 0."""
    found = []

    def walk(node):
        if isinstance(node, dict):
            for key, item in node.items():
                if key in USAGE_KEYS and type(item) in {int, float} and item == 0:
                    found.append(key)
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)
    walk(value)
    return found


def keys_named(value, wanted):
    found = []

    def walk(node):
        if isinstance(node, dict):
            for key, item in node.items():
                if key in wanted:
                    found.append(key)
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)
    walk(value)
    return found


def action_job(mind, memory, source, key="current"):
    memory.configure({"operational_lanes": True})
    return Appraisals(mind).enqueue([source(key)], "fixture-v1")["id"]


# --- One successful appraisal ------------------------------------------------------


def test_one_successful_appraisal_leaves_one_row_with_its_main_call(system, monkeypatch):
    mind, memory, source, _clock = system
    api = Api(monkeypatch, [DECISION], mind.engine)
    job = action_job(mind, memory, source)
    assert Appraisals(mind).run_one(api.provider, lane="action")["state"] == "complete"
    assert api.tools == ["submit_appraisal"]
    attempt, = rows(mind, job)
    assert (attempt["outcome"], attempt["ordinal"], attempt["lane"]) == ("committed", 1, "action")
    assert attempt["attempt_token"] == saved(mind, job)["attempt_token"] and attempt["charged"] is True
    call, = attempt["calls"]
    assert (call["purpose"], call["outcome"], call["usage_status"]) == ("appraise", "ok", "reported")
    assert call["usage"] == {"input_tokens": 11, "output_tokens": 1} and call["model"] == "deepseek-flash"
    assert call["request_id"] == "submit_appraisal-1" and attempt["usage_status"] == "reported"
    # A8: the call succeeded, so nothing about it is filed as a failed call.
    stored = saved(mind, job)
    assert "failed_call_receipt" not in stored and getattr(api.provider, "failure_receipt", "missing") is None
    # Digests, never bodies.
    assert attempt["proposal_digest"] and len(attempt["context_digest"]) == 64
    assert "Synthetic current decision" not in dumps(attempt)


def test_the_main_appraisal_call_now_reaches_the_metrics(system, monkeypatch):
    mind, memory, source, _clock = system
    api = Api(monkeypatch, [DECISION], mind.engine)
    action_job(mind, memory, source)
    Appraisals(mind).run_one(api.provider, lane="action")
    usage = metrics(mind, "structured_model_usage")
    assert [u["tool"] for u in usage] == ["submit_appraisal"]
    assert usage[0]["usage"] == {"input_tokens": 11, "output_tokens": 1} and usage[0]["usage_status"] == "reported"


# --- Every call inside one attempt, with its purpose --------------------------------


def test_a_schema_repair_and_an_expansion_round_are_recorded_with_their_purposes(system, monkeypatch):
    mind, memory, source, _clock = system
    memory.configure({"semantic_actions": True})
    faulty = {**DECISION, "values": {"initiative": "high"},
              "recall_needs": [{"query": "the earlier clock note", "reason": "Needed", "mode": "light"}]}
    api = Api(monkeypatch, [faulty, {**DECISION, "recall_needs": [{"query": "the earlier clock note",
                                                                   "reason": "Needed", "mode": "light"}]}, DECISION],
              mind.engine)
    job = action_job(mind, memory, source)
    assert Appraisals(mind).run_one(api.provider, lane="action")["state"] == "complete"
    assert api.tools[:2] == ["submit_appraisal", "repair_appraisal"]
    attempt, = rows(mind, job)
    assert attempt["outcome"] == "committed"
    assert purposes(attempt)[:3] == ["appraise", "schema-repair", "expansion"]
    assert [c["outcome"] for c in attempt["calls"][:3]] == ["schema-invalid", "ok", "ok"]
    assert all(c["usage_status"] == "reported" for c in attempt["calls"])


def test_a_session_advice_repair_is_its_own_call_and_is_never_filed_as_a_failure(system, monkeypatch):
    mind, _memory, source, _clock = system
    api = Api(monkeypatch, [INVENTED_ADVICE, {"action": "recall", "reason": "Thin context",
                                              "evidenceIds": ["observed-1"]}], mind.engine)
    jobs = Appraisals(mind, session_context=SESSION)
    job = jobs.enqueue([source("host-asks-for-a-session-review")], "fixture-v1",
                       origin="reflection", stimulus="session-maintenance")["id"]
    assert jobs.run_one(api.provider, job_id=job)["state"] == "complete"
    assert api.tools == ["submit_appraisal", "repair_session_advice"]
    attempt, = rows(mind, job)
    assert (attempt["outcome"], attempt["lane"]) == ("committed", "maintenance")
    assert purposes(attempt) == ["appraise", "advice-repair"]
    stored = saved(mind, job)
    assert "failed_call_receipt" not in stored
    assert stored["receipt"]["advice_repair"]["usage"]["output_tokens"] == 2


def test_compression_calls_belong_to_the_attempt_that_prepared_them(system, monkeypatch, long_evidence):
    mind, memory, source, _clock = system
    from kin_mind import appraisal
    monkeypatch.setattr(appraisal, "APPRAISAL_INPUT_BUDGET", 8000)
    api = Api(monkeypatch, [DECISION], mind.engine)
    memory.configure({"operational_lanes": True})
    job = Appraisals(mind).enqueue(long_evidence, "fixture-v1")["id"]
    assert Appraisals(mind).run_one(api.provider, lane="action")["state"] == "complete"
    assert api.tools[0] == "submit_compression" and api.tools[-1] == "submit_appraisal"
    attempt, = rows(mind, job)
    assert purposes(attempt)[0] == "compression" and purposes(attempt)[-1] == "appraise"
    assert all(c["usage_status"] == "reported" for c in attempt["calls"])


# --- Failed calls: a record and an explicit unknown, never a zero -------------------


@pytest.mark.parametrize("answer,outcome,error", [
    (500, "http-500", "deepseek-http-500"),
    (httpx.ConnectError("synthetic"), "network-error", "deepseek-network-error"),
    (httpx.ReadTimeout("synthetic"), "timeout", "deepseek-timeout"),
    (Unmetered(DECISION), "ok", None),
])
def test_a_call_without_usage_is_recorded_as_unknown_and_never_as_zero(system, monkeypatch, answer, outcome, error):
    mind, memory, source, _clock = system
    api = Api(monkeypatch, [answer], mind.engine)
    job = action_job(mind, memory, source)
    Appraisals(mind).run_one(api.provider, lane="action")
    attempt, = rows(mind, job)
    call, = attempt["calls"]
    assert (call["purpose"], call["outcome"], call["usage_status"]) == ("appraise", outcome, "unknown")
    assert call["usage"] is None and attempt["usage_status"] == "unknown"
    assert saved(mind, job).get("error") == error
    unknown = metrics(mind, "model_usage_unknown")
    assert [u["outcome"] for u in unknown] == [outcome] and unknown[0]["tool"] == "submit_appraisal"
    assert zeros_written_for_missing_usage([attempt, *model_metrics(mind)]) == []
    assert metrics(mind, "model_tokens") == []


def test_a_failed_repair_keeps_its_unknown_usage_beside_the_call_that_succeeded(system, monkeypatch):
    mind, memory, source, _clock = system
    api = Api(monkeypatch, [{**DECISION, "values": {"initiative": "high"}}, 500], mind.engine)
    job = action_job(mind, memory, source)
    assert Appraisals(mind).run_one(api.provider, lane="action")["state"] == "pending"
    attempt, = rows(mind, job)
    assert attempt["outcome"] == "failed" and purposes(attempt) == ["appraise", "schema-repair"]
    assert [c["usage_status"] for c in attempt["calls"]] == ["reported", "unknown"]
    assert attempt["usage_status"] == "partial-unknown"
    assert zeros_written_for_missing_usage([attempt, *model_metrics(mind)]) == []


def test_an_unpriced_role_is_reported_as_unpriced_and_a_silent_zero_is_never_metered(system, monkeypatch):
    mind, _memory, _source, _clock = system
    engine = mind.engine
    engine.settings("models", {
        "summary": ModelRole(endpoint="https://api.example.invalid", model="synthetic", protocol="anthropic").model_dump(),
        "conflict": ModelRole(endpoint="https://api.example.invalid", model="synthetic", protocol="anthropic",
                              input_price_per_million=3, output_price_per_million=9).model_dump()})
    replies = [{"usage": {"prompt_tokens": 7, "completion_tokens": 3}},
               {"usage": {"prompt_tokens": 7, "completion_tokens": 3}}, {}, 500]
    real = httpx.Client

    def respond(_request):
        answer = replies.pop(0)
        return httpx.Response(answer) if isinstance(answer, int) else httpx.Response(200, json=answer)
    monkeypatch.setattr(httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(respond), **kw))
    providers = Providers(engine)
    providers.request("summary", "messages", json_={})
    assert metrics(mind, "model_cost") == []
    unpriced = metrics(mind, "model_cost_unknown")
    assert len(unpriced) == 1 and unpriced[0]["cost_status"] == "unpriced" and unpriced[0]["cost"] is None
    assert metrics(mind, "model_tokens")[0]["value"] == 10
    providers.request("conflict", "messages", json_={})
    priced = metrics(mind, "model_cost")
    assert len(priced) == 1 and priced[0]["cost_status"] == "priced"
    assert priced[0]["value"] == pytest.approx((7 * 3 + 3 * 9) / 1_000_000)
    # A reply with no usage, then a refused request: both unknown, neither metered as zero.
    providers.request("summary", "messages", json_={})
    with pytest.raises(Exception):
        providers.request("summary", "messages", json_={})
    assert [u["outcome"] for u in metrics(mind, "model_usage_unknown")] == ["usage-not-reported", "http-500"]
    assert len(metrics(mind, "model_tokens")) == 2
    assert zeros_written_for_missing_usage(model_metrics(mind)) == []


# --- Attempts that leave no record of their own -------------------------------------


def test_a_killed_attempt_is_backfilled_as_abandoned_by_the_next_claimer(system, monkeypatch):
    mind, memory, source, _clock = system
    api = Api(monkeypatch, [DECISION], mind.engine)
    job = action_job(mind, memory, source)
    # A worker claimed the row and died: it is still `running`, and its lease has expired.
    killed = "attempt-token-of-the-killed-worker"
    with mind.engine.db.connect(write=True) as conn:
        data = {**saved(mind, job), "attempt_token": killed, "attempt_started_at": mind.clock()}
        conn.execute("UPDATE mind_appraisals SET state='running',lease=1,attempts=1,data=? WHERE id=?",
                     (dumps(data), job))
    assert Appraisals(mind).run_one(api.provider, lane="action")["state"] == "complete"
    abandoned, committed = sorted(rows(mind, job), key=lambda r: r["ordinal"])
    assert (abandoned["outcome"], abandoned["attempt_token"], abandoned["calls"]) == ("abandoned", killed, [])
    assert abandoned["usage_status"] == "unknown" and abandoned["error"] == "appraisal-attempt-abandoned"
    assert (committed["outcome"], committed["ordinal"]) == ("committed", 2)
    assert committed["attempt_token"] != killed


def test_an_attempt_that_lost_its_token_is_ledgered_as_discarded(system, monkeypatch):
    mind, memory, source, _clock = system

    class Stolen(Api):
        """Another worker re-claims the row while this attempt is talking to the model."""

        def respond(self, request):
            with mind.engine.db.connect(write=True) as conn:
                conn.execute("UPDATE mind_appraisals SET data=json_set(data,'$.attempt_token','someone-else') WHERE id=?",
                             (self.job,))
            return super().respond(request)
    api = Stolen(monkeypatch, [DECISION], mind.engine)
    api.job = job = action_job(mind, memory, source)
    Appraisals(mind).run_one(api.provider, lane="action")
    attempt, = rows(mind, job)
    assert attempt["outcome"] == "discarded" and purposes(attempt) == ["appraise"]
    assert metrics(mind, "appraisal_attempt_discarded")[0]["appraisal"] == job


def test_a_top_level_already_committed_attempt_is_ledgered_as_discarded(system, monkeypatch):
    mind, memory, source, _clock = system
    committed = {"event_id": "mind_fixture_committed_elsewhere", "revision": 9}

    class Racing(Api):
        """A parallel worker's commit lands while this attempt is still talking to the model."""

        def respond(self, request):
            answer = super().respond(request)
            with mind.engine.db.connect(write=True) as conn:
                conn.execute("INSERT OR IGNORE INTO commands VALUES(?,?,?)",
                             (mind._key(self.job), "another-attempt-digest", dumps(committed)))
            return answer
    api = Racing(monkeypatch, [DECISION], mind.engine)
    api.job = job = action_job(mind, memory, source)
    result = Appraisals(mind).run_one(api.provider, lane="action")
    assert (result["state"], result["completed_from"]) == ("complete", "already-committed")
    attempt, = rows(mind, job)
    # The row is complete, but nothing was committed by this attempt and it paid for one call.
    assert attempt["outcome"] == "discarded" and purposes(attempt) == ["appraise"]
    assert attempt["charged"] is True and attempt["usage_status"] == "reported"


# --- The rendered prompt ------------------------------------------------------------


def test_a_compressed_prompt_carries_no_compression_receipt_usage_or_request_id(system, monkeypatch, long_evidence):
    mind, memory, source, _clock = system
    from kin_mind import appraisal
    monkeypatch.setattr(appraisal, "APPRAISAL_INPUT_BUDGET", 8000)
    memory.configure({"operational_lanes": True})
    api = Api(monkeypatch, [DECISION], mind.engine)
    job = Appraisals(mind).enqueue(long_evidence, "fixture-v1")["id"]
    assert Appraisals(mind).run_one(api.provider, lane="action")["state"] == "complete"
    assert api.tools == ["submit_compression", "submit_appraisal"]
    sent = [r for r in api.requests if r["tool"] == "submit_appraisal"]
    assert keys_named(sent, PROMPT_FORBIDDEN) == []
    assert "submit_compression-1" not in dumps(sent)
    # The receipt is kept, in the attempt's calls and on the queue row; only not in the prompt.
    attempt, = rows(mind, job)
    assert purposes(attempt) == ["compression", "appraise"]
    assert saved(mind, job)["receipt"]["compression_receipt"][0]["request_id"] == "submit_compression-1"


def test_an_expansion_prompt_carries_no_retrieval_receipts_or_usage(system, monkeypatch):
    mind, memory, source, _clock = system
    memory.configure({"semantic_actions": True})
    recall = {"query": "the earlier clock note", "reason": "Needed", "mode": "light"}
    api = Api(monkeypatch, [{**DECISION, "recall_needs": [recall]}, DECISION], mind.engine)
    job = action_job(mind, memory, source)
    assert Appraisals(mind).run_one(api.provider, lane="action")["state"] == "complete"
    first, second = [r["content"] for r in api.requests if r["tool"] == "submit_appraisal"]
    assert keys_named([first, second], PROMPT_FORBIDDEN) == []
    # The expansion adds what was recalled and how much budget is left, and nothing else.
    added = {k: v for k, v in second.items() if k not in first}
    assert set(added) == {"requested_memory", "recall_budget"}
    assert set(added["requested_memory"][0]["retrieval"]) & PROMPT_FORBIDDEN == set()
    assert "submit_appraisal-1" not in dumps(second)
    attempt, = rows(mind, job)
    assert purposes(attempt) == ["appraise", "expansion"]
    assert saved(mind, job)["receipt"]["memory_expansion"]["rounds"] == 1


# --- The switch, and the operator view ----------------------------------------------


def test_the_switch_off_writes_no_ledger_row_while_the_fixes_stay_in_force(system, monkeypatch):
    mind, memory, source, _clock = system
    memory.configure({"attempt_ledger": False, "semantic_actions": True})
    recall = {"query": "the earlier clock note", "reason": "Needed", "mode": "light"}
    api = Api(monkeypatch, [{**DECISION, "recall_needs": [recall]}, DECISION, Unmetered(DECISION)], mind.engine)
    job = action_job(mind, memory, source)
    assert Appraisals(mind).run_one(api.provider, lane="action")["state"] == "complete"
    assert rows(mind) == []
    # A8, A9 and the receipts leaving the prompt are defect fixes, not optimizations:
    # they do not follow the switch.
    assert "failed_call_receipt" not in saved(mind, job)
    assert keys_named([r["content"] for r in api.requests], PROMPT_FORBIDDEN) == []
    second = Appraisals(mind).enqueue([source("later")], "fixture-v1")["id"]
    Appraisals(mind).run_one(api.provider, lane="action")
    assert rows(mind) == [] and saved(mind, second)["receipt"]["usage_status"] == "unknown"
    assert [u["outcome"] for u in metrics(mind, "model_usage_unknown")] == ["ok"]
    assert zeros_written_for_missing_usage(model_metrics(mind)) == []


def test_the_host_exposes_the_ledger_read_only_and_without_private_text(system, monkeypatch, tmp_path):
    mind, memory, source, _clock = system
    api = Api(monkeypatch, [DECISION], mind.engine)
    job = action_job(mind, memory, source)
    Appraisals(mind).run_one(api.provider, lane="action")
    config = {"root": str(mind.engine.db.root), "scope": mind.scope.model_dump(), "agent_version": "fixture-v1",
              "session_id": "synthetic-session"}
    answer = dispatch(config, "appraisal-attempts", {"job_id": job})
    assert [a["outcome"] for a in answer["attempts"]] == ["committed"]
    assert answer["attempts"][0]["appraisal_id"] == job
    assert "Synthetic current decision" not in dumps(answer)
    assert dispatch(config, "appraisal-attempts", {})["attempts"] == answer["attempts"]
    with pytest.raises(ValueError, match="Attempt ledger limit"):
        dispatch(config, "appraisal-attempts", {"limit": 0})


def test_every_tool_the_appraisal_path_calls_is_registered_with_a_purpose():
    """A call added inside an attempt has to name what it is for, not fall back to `other`."""
    import ast
    from pathlib import Path

    from kin_mind import adaptive_recall, appraisal, context, decision_context, revalidation
    names = set()
    for module in (appraisal, context, adaptive_recall, decision_context, revalidation):
        for node in ast.walk(ast.parse(Path(module.__file__).read_text())):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "structured"
                    and node.args and isinstance(node.args[0], ast.Constant)):
                names.add(node.args[0].value)
    assert {"repair_appraisal", "repair_session_advice", "repair_sharing", "submit_compression",
            "submit_recall_ranking", "revalidate_appraisal"} <= names <= set(ledger.TOOL_PURPOSES)
    assert set(ledger.TOOL_PURPOSES.values()) | {"other"} == set(ledger.PURPOSES)


def test_an_unknown_outcome_is_refused_rather_than_stored(system):
    mind, _memory, _source, _clock = system
    with pytest.raises(ValueError, match="appraisal attempt outcome"):
        ledger.record(mind.engine, mind.scope.key(),
                      {"appraisal_id": "job", "attempt_token": "t", "outcome": "invented"})


@pytest.fixture
def long_evidence(system):
    """Sources whose combined text no longer fits one request, so the host compresses first."""
    mind, _memory, _source, _clock = system
    from eventmem.core.models import SourceInput
    paragraph = "A synthetic paragraph about a small illustrated clock and its escapement. " * 24
    return [mind.engine.receive(SourceInput(
        namespace="synthetic", key="long-evidence-" + str(part), scope=mind.scope, occurred_at=mind.clock(),
        text="\n\n".join(paragraph + str(part) + ":" + str(index) for index in range(15)),
        metadata={"role": "user", "host_event": "message"}))["id"] for part in range(3)]