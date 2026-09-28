"""Nothing a model is shown carries a source's metadata (the owner's decision, 2026-09-28:
来源的元数据只做为索引检索，不应该注入哈，也不必要引用，只需要可以溯源就行).

`kin_mind.model_view` shows every evidence reference as its trace and every source shown as an item
without its metadata -- the facts a prompt reads from it under their own names -- and it is enforced
once at each boundary a model is on the other side of. Asserted here: the rule itself (full and slim
references alike, JSON carried as text, what is left exactly as it was); the core MCP server, every
tool of it, and the three reads the owner named by the rows they read (a graph read, an event thread
at every detail, the plans with their runs and history); the host actions whose answers the host puts
in front of a model; a prepared context injection; every DeepSeek request (a structured call, the
appraisal's own request and its projection, a main-session fork's, eventmem's providers); and the
exploration executor's prompt and input files. And that nothing but a reference's dropped fields is
taken from a graph node, a share, a plan, a run or an exploration: no false positive.

Synthetic replays only: an injected clock, sources through the engine, mock transports, no network."""
import asyncio
import json

import httpx
from test_evidence_refs import AGENT, MARKER, create, every_section, refs_in, rich
from test_exploration_decision_archive import explored

from eventmem.core.db import dumps
from eventmem.core.mcp import create_mcp
from kin_mind import evidence_refs
from kin_mind.appraisal import APPRAISAL_MODEL, SYSTEM, DeepSeek, SharingReview, appraisal_context
from kin_mind.exploration_decisions import SharingDecision
from kin_mind.memory import MemoryContinuity
from kin_mind.model_view import DROPPED, SOURCE_FACTS, for_model, index_only, is_source, leaks

pytest_plugins = ("test_kin_mind", "test_autonomous_plans")

SID = "src_" + "a" * 32
RID = "mem_" + "b" * 32
PAGE = "src_" + "c" * 32


def full_ref(**extra):
    """A reference as `Mind._reference` writes one, with a copy of its source's metadata."""
    return {"source_id": SID, "record_id": RID, "hash": "h" * 64, "revision": 2, "namespace": "kin-exploration",
            "source_key": "explore_x", "occurred_at": "2026-09-01T00:00:00+00:00", "authority": "model",
            "session": "session-x", "received_at": "2026-09-01T00:00:01+00:00",
            "metadata": {"host_event": "exploration-result", "exploration_id": "explore_x", "observation_ids": [PAGE],
                         "sources": [{"title": f"{MARKER} page", "url": "https://example.com"}]}, **extra}


def source_item(**extra):
    """A source shown as an item, as an appraisal is shown what is under review."""
    return {"id": SID, "authority": "model", "revision": 1, "occurred_at": "2026-09-01T00:00:00+00:00",
            "received_at": "2026-09-01T00:00:01+00:00", "text": "what the exploration found",
            "metadata": {"host_event": "exploration-result", "role": "assistant", "exploration_id": "explore_x",
                         "channel": "wechat", "observation_ids": [PAGE], "title": f"{MARKER} title"}, **extra}


def config_for(mind):
    return {"root": str(mind.engine.db.root), "scope": mind.scope.model_dump(), "agent_version": AGENT,
            "session_id": "synthetic-session"}


def traced(ref):
    """A reference shown as its trace: its ids and versions, nothing it drops."""
    return {"source_id", "record_id", "hash", "revision"} <= set(ref) and not set(ref) & set(DROPPED)


def clean(value):
    """Nothing `for_model` would still take out, and none of a source's metadata's words."""
    return not leaks(value) and MARKER not in dumps(value) and "runtime-" not in dumps(value)


# --- the rule --------------------------------------------------------------------------------------

def test_a_reference_is_shown_as_its_trace_full_or_slim_alike():
    full = full_ref(exploration_id="explore_x")
    slim = evidence_refs.trace(full)
    assert for_model(full) == for_model(slim) == slim
    assert set(slim) == set(full) - set(DROPPED) and "exploration_id" in slim, "a field beside the trace stays"
    assert for_model({"erased": True, **full})["erased"] is True
    assert full["metadata"], "the original is left as it is"
    nested = {"plans": [{"steps": [{"decision": {"evidence": [full]}}]}], "targets": (full,)}
    shown = for_model(nested)
    assert shown["plans"][0]["steps"][0]["decision"]["evidence"] == [slim] and shown["targets"] == (slim,)
    assert leaks(nested) and not leaks(shown)


def test_a_source_is_shown_with_the_facts_a_prompt_reads_under_their_own_names():
    item = source_item()
    shown = for_model({"new_evidence": [item]})["new_evidence"][0]
    assert "metadata" not in shown and MARKER not in dumps(shown) and PAGE not in dumps(shown)
    assert {key: shown[key] for key in SOURCE_FACTS} == {"host_event": "exploration-result", "role": "assistant",
                                                        "exploration_id": "explore_x"}
    assert "channel" not in shown, "only the named facts"
    # Its own fields stay as they are, and a fact it names itself is not overwritten.
    assert {k: shown[k] for k in ("id", "authority", "revision", "occurred_at", "received_at", "text")} == \
        {k: item[k] for k in ("id", "authority", "revision", "occurred_at", "received_at", "text")}
    assert for_model(source_item(role="user"))["role"] == "user"
    assert for_model(source_item(metadata="[erased]")) == {k: v for k, v in source_item().items() if k != "metadata"}
    # Not a source: another kind of id, or no metadata.
    assert is_source(source_item()) and not is_source({**source_item(), "id": RID})
    assert not is_source({k: v for k, v in source_item().items() if k != "metadata"})
    assert not is_source(full_ref(id=SID)), "a reference is a reference"


def test_json_carried_as_text_is_shown_the_same_way_and_any_other_text_exactly_as_it_is():
    whole = json.dumps({"evidence": [full_ref()]}, ensure_ascii=False, indent=2)
    shown = for_model(whole)
    assert json.loads(shown) == {"evidence": [evidence_refs.trace(full_ref())]} and not leaks(shown)
    lines = "\n".join(["记忆资料：", dumps({"id": "c1", "text": dumps({"evidence": [full_ref()]})}),
                       dumps({"id": "c2", "text": "plain"}), "[not json"])
    shown = for_model(lines)
    assert leaks(lines) and not leaks(shown) and MARKER not in shown
    assert shown.split("\n")[0] == "记忆资料：" and shown.split("\n")[2:] == lines.split("\n")[2:]
    # A text that names nothing taken out is never parsed, and never changed.
    for text in ('{"id": "c3",  "revision": 1}', dumps({"source_id": SID, "record_id": RID, "hash": "h"}),
                 "[" * 30000, '{"metadata": ', ""):
        assert for_model(text) is text or for_model(text) == text
    # A JSON text holding a reference that has nothing to drop is left byte for byte.
    slim_text = json.dumps({"evidence": [evidence_refs.trace(full_ref())], "session": "x"}, indent=1)
    assert for_model(slim_text) == slim_text


def test_what_is_taken_out_is_what_a_read_is_not_taken_to_rest_on():
    ref, item = full_ref(), source_item()
    assert all(index_only(ref, key) for key in DROPPED)
    assert not any(index_only(ref, key) for key in evidence_refs.TRACE)
    assert index_only(item, "metadata") and not index_only(item, "id") and not index_only(item, "text")
    assert not index_only({"id": "node-1", "metadata": {"x": PAGE}}, "metadata"), "only a source's own"
    assert not index_only({"session": PAGE}, "session"), "only a reference's"


# --- the core MCP server ---------------------------------------------------------------------------

def rich_store(mind, clock):
    """An exploration's graph node with a finding of it delivered as a share, an event related to it,
    a wish and a plan -- each resting on a source whose metadata names pages and carries words
    (`MARKER`), with the references as the store keeps them before the migration: full."""
    memory = MemoryContinuity(mind)
    memory.configure({"records": True, "semantic": True, "context": True, "graph": True, "sharing": True,
                      "graph_recall": True, "autonomous_plans": True})
    result, observed = explored(mind, clock, "explore_sea", pages=2)
    said = rich(mind, clock, "sea", "合成的约定：周末一起去看海")
    with mind.engine.db.connect(write=True) as conn:
        graph = memory.graph
        graph._put(conn, {"id": "explore_sea", "kind": "exploration", "title": "合成的海边灯塔", "text": "灯塔的发现",
                          "source_ids": [result], "evidence": graph.proof(conn, [result]), "basis": "documented"})
        graph._put(conn, {"id": "event-sea", "kind": "event", "title": "一起去看海", "text": "周末看海的约定",
                          "source_ids": [said], "evidence": graph.proof(conn, [said, result]), "basis": "documented"})
        graph.link(conn, "event-sea", "related", "explore_sea", graph.proof(conn, [said]), basis="documented",
                   reason="合成的关联", event_id="event-sea")
        units = memory.sharing.units(conn, "explore_sea", ["海边的灯塔还亮着。"], [result])
    memory.ingest({"id": "delivery-sea", "kind": "delivery", "at": mind.clock(), "channel": "wechat", "delivery_id": "batch-sea",
                   "bubble_id": "one", "text": units[0]["text"], "state": "accepted", "message_id": "receipt-sea",
                   "references": [{"unit_id": units[0]["id"], "version": 1, "mode": "new"}]})
    create(mind, clock, "wish-sea", [result])
    from kin_mind.plans import AutonomousPlans
    AutonomousPlans(mind).manage({"command_id": "create:sea", "action": "create", "key": "sea", "goal": "准备看海",
                                  "motivation": "一起的计划", "reason": "有来源的计划", "evidence_ids": [said, result],
                                  "steps": [{"id": "list", "actor": "create", "goal": "列清单", "completion": "清单保存"}]})
    return result, observed, said


def tools_of(server):
    return {tool.name: tool for tool in server._tool_manager.list_tools()}


def call(server, name, **arguments):
    return asyncio.run(server.call_tool(name, arguments))


def answer(result):
    return result[1] if isinstance(result, tuple) else json.loads(result[0].text)


def test_every_core_tool_answers_as_a_model_may_be_shown_it(setup):
    mind, _, clock = setup
    result, observed, said = rich_store(mind, clock)
    server = create_mcp(mind.engine)
    tools = tools_of(server)
    assert tools and all(getattr(tool.fn, "shown_to_model", False) for tool in tools.values()), \
        [name for name, tool in tools.items() if not getattr(tool.fn, "shown_to_model", False)]
    # The signature every tool was declared with is what the server still reads.
    assert set(tools["read_event_thread"].parameters["properties"]) >= {"scope", "identifier", "detail", "budget"}
    scope = mind.scope.model_dump()
    with mind.engine.db.connect() as conn:
        stored = conn.execute("SELECT data FROM mind_graph_nodes WHERE id='event-sea'").fetchone()[0]
    assert MARKER in stored and leaks(json.loads(stored)), "the graph keeps its full references"
    reads = {
        "read_graph": {"scope": scope, "focus": "event-sea", "hops": 2},
        "read_graph ": {"scope": scope, "query": "看海"},
        "read_autonomous_plans": {"scope": scope, "history": True},
        "read_affective_state": {"scope": scope, "history": 3},
        "source_evidence": {"source_id": result},
        "read_memory": {"record_id": mind.engine.source(said)["record_ids"][0]},
        "memory_history": {"record_id": mind.engine.source(said)["record_ids"][0]},
        **{f"read_event_thread {detail}": {"scope": scope, "identifier": "event-sea", "detail": detail, "budget": 8000}
           for detail in ("index", "summary", "original")},
    }
    for label, arguments in reads.items():
        shown = answer(call(server, label.split(" ")[0], **arguments))
        assert clean(shown), (label, leaks(shown))
    graph = answer(call(server, "read_graph", scope=scope, focus="event-sea", hops=2))
    node = next(n for n in graph["nodes"] if n["id"] == "event-sea")
    # Traceable still: every reference names its source, record, revision and hash.
    assert {ref["source_id"] for ref in node["evidence"]} == {said, result}
    assert all(traced(ref) for ref in node["evidence"])
    assert graph["edges"] and all(traced(ref) for e in graph["edges"] for ref in e["evidence"])
    plans = answer(call(server, "read_autonomous_plans", scope=scope, history=True))["plans"]
    assert plans[0]["evidence"] and plans[0]["history"] and not leaks(plans)
    # A root record read by id: its attributes without its copy of its source's metadata, but with
    # the facts a prompt reads and anything the source did not give it.
    root = mind.engine.source(said)["record_ids"][0]
    with mind.engine.db.connect() as conn:
        stored = json.loads(conn.execute("SELECT data FROM records WHERE id=?", (root,)).fetchone()[0])["attributes"]
    assert stored["title"].startswith(MARKER) and stored["runtime_event_id"] == "runtime-sea"
    record = answer(call(server, "read_memory", record_id=root))
    assert record["attributes"] == {k: v for k, v in stored.items() if k in {"role", "host_event"} or k not in
                                    {"channel", "title", "runtime_event_id"}}
    assert record["attributes"]["role"] == "user" and "channel" not in record["attributes"]
    # A source read by id: its facts, by name; its metadata's pages and words, not at all.
    source = answer(call(server, "source_evidence", source_id=result))
    assert source["host_event"] == "exploration-result" and source["exploration_id"] == "explore_sea"
    assert "metadata" not in source and not set(observed) & set(json.dumps(source).split('"'))


# --- the host's actions ----------------------------------------------------------------------------

def test_every_action_whose_answer_the_host_puts_before_a_model_is_shown_as_one(setup, monkeypatch):
    from kin_mind import host
    mind, _, clock = setup
    leaky = {"state": {"desires": [{"id": "d1", "evidence": [full_ref()]}]}, "new_evidence": [source_item()],
             "text": dumps({"evidence": [full_ref()]})}
    monkeypatch.setattr(host, "_dispatch", lambda config, action, request: json.loads(dumps(leaky)))
    prompt_bound = {"memory-context", "read", "ingest", "state-overview", "share-history", "work-history", "graph",
                    "graph-detail", "event-thread", "autonomous-plans", "procedure-memory", "traits", "candidate",
                    "claim", "plan-claim", "prepare-exploration", "session-checkpoint"}
    assert host.MODEL_FACING == prompt_bound
    for action in sorted(prompt_bound):
        shown = host.dispatch(config_for(mind), action, {})
        assert not leaks(shown) and shown["new_evidence"][0]["exploration_id"] == "explore_x", action
    # What the host keeps to itself, or hands back to the store, is answered as it was.
    for action in ("review", "settle", "session-snapshot", "session-validate", "operational-status", "explore"):
        assert host.dispatch(config_for(mind), action, {}) == leaky, action


def test_the_host_reads_of_a_rich_store_are_clean(setup):
    from kin_mind.host import dispatch
    mind, _, clock = setup
    rich_store(mind, clock)
    config = config_for(mind)
    for action, request in (("graph", {"focus": "event-sea", "hops": 2}), ("graph-detail", {"identifier": "event-sea"}),
                            ("event-thread", {"identifier": "event-sea", "detail": "original", "budget": 8000}),
                            ("autonomous-plans", {"history": True}), ("candidate", {}),
                            ("memory-context", {"query": "看海", "purpose": "chat", "budget": 8000}),
                            ("read", {"history": 3}), ("state-overview", {"query": "看海"})):
        shown = dispatch(config, action, request)
        assert clean(shown), (action, leaks(shown))


def test_the_explorations_the_state_read_and_an_ingest_hand_the_host_are_shown_as_a_model_may_see_them(setup):
    """`read` and, with the memory context off, `ingest` hand the host the latest explorations
    (`findings`, Explorations.recent) beside the state, outside `Mind.read`: a run that kept full
    references and a source with its metadata is handed over with neither, and still names them."""
    from kin_mind.host import dispatch
    mind, _, clock = setup
    result, observed = explored(mind, clock, "explore_kept", pages=2)
    with mind.engine.db.connect(write=True) as conn:
        [ref] = mind._evidence(conn, [result])
        row = json.loads(conn.execute("SELECT data FROM mind_explorations WHERE id='explore_kept'").fetchone()[0])
        conn.execute("UPDATE mind_explorations SET data=? WHERE id='explore_kept'", (dumps(
            {**row, "evaluated_sources": [ref], "shown_sources": [{"id": result, "text": "茶", "metadata": ref["metadata"]}]}),))
    assert leaks(ref) and "a page explore_kept cited" in dumps(ref)
    config = config_for(mind)
    for action, request in (("read", {}), ("ingest", {"id": "owner-1", "text": "合成的消息", "at": mind.clock(), "channel": "wechat"})):
        if action == "ingest":
            MemoryContinuity(mind).configure({"context": False})
        shown = dispatch(config, action, request)
        [run] = [f for f in shown["findings"] if f["id"] == "explore_kept"]
        assert not leaks(shown) and "a page explore_kept cited" not in dumps(shown), action
        assert not set(observed) & set(dumps(run["evaluated_sources"] + run["shown_sources"]).split('"')), action
        assert run["evaluated_sources"][0]["source_id"] == result and run["shown_sources"][0]["exploration_id"] == "explore_kept"


def test_a_prepared_injection_is_named_and_hashed_as_it_is_shown(setup):
    from kin_mind.context import Contexts
    from kin_mind.context_delivery import ContextDelivery, text_hash
    mind, _, _ = setup
    delivery = ContextDelivery(Contexts(mind))
    text = "共享记忆资料：\n" + dumps({"id": "c1", "text": dumps({"evidence": [full_ref()]})})
    prepared = delivery.prepare("thread-shown", "initial", "event-1", text, [])
    body = prepared["text"]
    assert not leaks(body) and MARKER not in body and prepared["text_hash"] == text_hash(body)
    assert body.endswith(for_model(text))
    plain = delivery.prepare("thread-shown", "initial", "event-2", "plain words", [])
    assert plain["text"].endswith("\nplain words")


# --- every DeepSeek request ------------------------------------------------------------------------

class Endpoint:
    """DeepSeek's messages endpoint: keeps every request as sent and answers with one tool call."""

    def __init__(self, monkeypatch, name, result):
        monkeypatch.setenv("KIN_TEST_DS_KEY", "synthetic")
        self.sent, self.name, self.result = [], name, result
        self.provider = DeepSeek("https://api.deepseek.com", APPRAISAL_MODEL, "KIN_TEST_DS_KEY", timeout=60,
                                 transport=httpx.MockTransport(self.respond))

    def respond(self, request):
        self.sent.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "req-1", "model": APPRAISAL_MODEL, "stop_reason": "end_turn",
                                         "usage": {"input_tokens": 10, "output_tokens": 5},
                                         "content": [{"type": "tool_use", "id": "toolu_1", "name": self.name,
                                                      "input": self.result}]})


def test_a_structured_call_asks_with_nothing_taken_out_and_repair_sharing_keeps_what_it_decides_by(setup, monkeypatch):
    """`repair_sharing` hands the model the exploration targets (references, with the exploration's id
    beside them) and the results under review (sources): both as shown, the ids it decides by kept."""
    mind, _, clock = setup
    result, observed = explored(mind, clock, "explore_tea", pages=2)
    with mind.engine.db.connect() as conn:
        [ref] = mind._evidence(conn, [result])
    assert ref["metadata"]["observation_ids"] == observed
    endpoint = Endpoint(monkeypatch, "repair_sharing",
                        {"sharing": [{"exploration_id": "explore_tea", "decision": "keep", "reason": "记着"}]})
    endpoint.provider.engine = mind.engine
    context = {"clock": {"current_time": mind.clock()}, "exploration_targets": [{**ref, "exploration_id": "explore_tea"}],
               "new_evidence": [{"id": result, "authority": "model", "revision": 1, "occurred_at": ref["occurred_at"],
                                 "received_at": ref["received_at"], "metadata": ref["metadata"], "text": "茶的发现"}],
               "recent_dialogue": []}
    proposal = SharingReview(sharing=[SharingDecision(exploration_id="explore_tea", decision="keep", reason="先记着")])
    decided, _ = endpoint.provider.repair_sharing(proposal, context)
    assert decided[0].exploration_id == "explore_tea"
    [sent] = endpoint.sent
    asked = json.loads(sent["messages"][0]["content"])
    assert not leaks(sent) and not leaks(asked) and not set(observed) & set(dumps(asked).split('"'))
    assert asked["targets"][0]["exploration_id"] == "explore_tea" and asked["targets"][0]["source_id"] == result
    assert asked["results"][0]["exploration_id"] == "explore_tea" and asked["results"][0]["id"] == result
    assert "metadata" not in asked["results"][0]
    assert context["exploration_targets"][0]["metadata"], "the caller's context is left as it is"


def test_the_appraisal_request_carries_nothing_taken_out_and_the_models_own_turns_go_back_as_they_came(monkeypatch):
    endpoint = Endpoint(monkeypatch, "submit_appraisal", {"reason": "x"})
    thinking = {"type": "thinking", "thinking": dumps({"evidence": [full_ref()]}), "signature": "sig-1"}
    messages = [{"role": "user", "content": dumps({"state": {"desires": [{"evidence": [full_ref()]}]}})},
                {"role": "assistant", "content": [thinking]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_0",
                                              "content": dumps({"items": [source_item()]})}]}]
    endpoint.provider._post("system", messages, [], 30, lambda *a, **k: None)
    [sent] = endpoint.sent
    assert not leaks(sent["messages"][0]) and not leaks(sent["messages"][2]) and MARKER not in dumps(sent["messages"][2])
    assert sent["messages"][1]["content"] == [thinking], "a signed thinking block is sent back unchanged"
    assert leaks(messages[0]), "the exchange the provider keeps is left as it is"


def test_the_appraisal_projection_shows_what_is_under_review_by_its_facts(setup):
    mind, _, clock = setup
    result, _ = explored(mind, clock, "explore_moon")
    with mind.engine.db.connect() as conn:
        [ref] = mind._evidence(conn, [result])
    context = {"state": mind.read(), "stimulus": "exploration-result",
               "new_evidence": [{"id": result, "authority": "model", "revision": 1, "metadata": ref["metadata"], "text": "月亮"}],
               "exploration_targets": [{**ref, "exploration_id": "explore_moon"}]}
    shown = appraisal_context(context)
    assert not leaks(shown) and "a page explore_moon cited" not in dumps(shown)
    assert shown["new_evidence"][0]["exploration_id"] == "explore_moon" and shown["new_evidence"][0]["host_event"] == "exploration-result"
    assert shown["exploration_targets"][0]["exploration_id"] == "explore_moon"
    # The prompt reads the exploration's id where it now is, and says what the named facts are.
    assert "new_evidence.metadata" not in SYSTEM and "new_evidence 来源的 exploration_id" in SYSTEM
    assert "host_event 是宿主事件类别" in SYSTEM


def test_a_main_session_fork_is_asked_with_nothing_taken_out(setup):
    from test_main_session_review import native_provider
    mind, _, _ = setup
    asked = []

    def exchange(request):
        asked.append(request)
        return {"state": "complete", "result": {"sharing": []},
                "receipt": {"native_turn_id": "turn-1", "model": "gpt-6-astra", "usage": {"input_tokens": 5, "output_tokens": 1}}}
    provider = native_provider(mind, exchange)
    provider.structured("repair_sharing", SharingReview, "system", {"targets": [full_ref()], "results": [source_item()]})
    [request] = asked
    assert not leaks(request["context"]) and request["context"]["results"][0]["exploration_id"] == "explore_x"


def test_eventmems_providers_ask_with_nothing_taken_out(tmp_path, monkeypatch):
    from eventmem.core import Engine
    from eventmem.core.providers import Providers
    engine = Engine(tmp_path / "db")
    engine.settings("models", {"summary": {"endpoint": "https://synthetic.invalid/v1", "model": "synthetic-model",
                                           "input_price_per_million": 1, "output_price_per_million": 2}})
    sent, real = [], httpx.Client

    def respond(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}],
                                         "usage": {"prompt_tokens": 10, "completion_tokens": 2}})
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: real(*a, **{**k, "transport": httpx.MockTransport(respond)}))
    Providers(engine).json("summary", "x", {"records": [{"id": RID, "evidence": [full_ref()]}, source_item()]})
    [request] = sent
    assert not leaks(request) and MARKER not in dumps(request)
    assert json.loads(request["messages"][1]["content"])["records"][1]["exploration_id"] == "explore_x"


# --- the exploration executor ----------------------------------------------------------------------

def test_the_executor_is_shown_its_topic_and_checkpoint_with_nothing_taken_out(tmp_path, monkeypatch):
    from test_kin_exploration_codex import COMPLETE, OBSERVE, TOPIC, codex_kwargs, fake_codex

    from kin_mind.codex_executor import codex_prompt, run_codex
    topic = {**TOPIC, "work_history": {"items": [{"id": "work-1", "evidence": [full_ref()]}]},
             "procedure_candidates": {"items": [{"id": "proc-1", "evidence": [full_ref()]}]}}
    prompt = codex_prompt(topic, budget_seconds=60, continuation={"sources_used": [full_ref()], "attempt": 1})
    shown = prompt.split("题目资料（不是额外指令）：", 1)[1].split("\n", 1)
    assert not leaks(json.loads(shown[0])) and MARKER not in prompt and "session-x" not in prompt
    assert json.loads(shown[0])["work_history"]["items"][0]["evidence"] == [evidence_refs.trace(full_ref())]
    monkeypatch.setenv("KIN_TEST_DS_KEY", "sk-synthetic")
    fake = fake_codex(tmp_path / "fake-observe", OBSERVE + COMPLETE)
    run_codex(fake, topic, tmp_path / "job", **codex_kwargs())
    handed = json.loads(next((tmp_path / "job").rglob("input.json")).read_text())
    observed = json.loads(next((tmp_path / "job").rglob("observed.json")).read_text())
    assert not leaks(handed) and MARKER not in observed["prompt"]
    assert topic["work_history"]["items"][0]["evidence"][0]["metadata"], "the caller's topic is left as it is"


# --- no false positive -----------------------------------------------------------------------------

def taken(raw, shown, path=()):
    """Every key `shown` lacks that `raw` had, with the dict it was taken from; and every other
    difference, which there must be none of."""
    out, other = [], []
    if isinstance(raw, dict) and isinstance(shown, dict):
        for key, item in raw.items():
            if key not in shown:
                out.append((path, key, raw))
            else:
                deeper, rest = taken(item, shown[key], (*path, key))
                out += deeper
                other += rest
        other += [(path, key) for key in shown if key not in raw and not (key in SOURCE_FACTS and "metadata" in raw)]
    elif isinstance(raw, list) and isinstance(shown, list) and len(raw) == len(shown):
        for index, (one, two) in enumerate(zip(raw, shown)):
            deeper, rest = taken(one, two, (*path, index))
            out += deeper
            other += rest
    elif raw != shown:
        other.append(path)
    return out, other


def test_a_graph_node_a_share_a_plan_and_an_exploration_lose_only_their_references_dropped_fields(setup):
    """No dict that is not a reference -- a graph node or edge, a share and its coverage, a plan with
    its steps and history, an exploration run -- loses a field, and none gains one; a reference loses
    only what it drops, and is one `Mind._reference` wrote."""
    from kin_mind.exploration import Explorations
    from kin_mind.plans import AutonomousPlans
    mind, _, clock = setup
    rich_store(mind, clock)
    every_section(mind, clock)
    memory = MemoryContinuity(mind)
    reads = {"graph": memory.graph.read(focus="event-sea", hops=2),
             "graph by query": memory.graph.read(query="看海"),
             "plans": AutonomousPlans(mind).read(history=True),
             "shares": memory.history("share", limit=8), "works": memory.history("work", limit=8),
             "explorations": Explorations(mind).recent(8),
             "nodes decorated": [memory.sharing.decorate(n) for n in memory.graph.read(query="海")["nodes"]]}
    removed = 0
    for label, raw in reads.items():
        out, other = taken(raw, for_model(raw))
        assert not other, (label, other)
        for path, key, container in out:
            assert key in DROPPED and evidence_refs.is_ref(container) and type(container.get("revision")) is int, (label, path, key)
            assert path[-2] in {"evidence", "configuration_evidence", "resolution_evidence"}, (label, path)
        removed += len(out)
    assert removed, "the rows read kept full references to take the metadata from"
    assert reads["shares"]["items"] and any("share_coverage" in n for n in reads["nodes decorated"])
    assert any(refs_in(reads["plans"])) and any(refs_in(reads["graph"]))
