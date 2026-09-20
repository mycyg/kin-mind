"""Controlled Computer Use policy and receipts; no live user windows in unit tests."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from kin_mind.computer_use import (
    MAX_STATE,
    ComputerUseController,
    CuaBackend,
    DeepSeekActionReviewer,
    _backend_environment,
    create_server,
)
from kin_mind.source_ledger import (
    build_ledger,
    seal_source_receipt,
    valid_computer_receipt,
    validate_continuation_sources,
)


class FakeBackend:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def call_json(self, code, title):
        self.calls.append((code, title))
        if not self.responses:
            raise AssertionError("unexpected CUA call")
        return self.responses.pop(0)


class FakeReviewer:
    def __init__(self, *, decision="allow", category="local_reversible"):
        self.decision = decision
        self.category = category
        self.requests = []

    async def review(self, context):
        self.requests.append(context)
        return {
            "decision": self.decision, "category": self.category,
            "effect": "classified effect", "target": "classified target",
            "reason": "synthetic independent review",
            "snapshot_hash": context["snapshot_hash"],
            "input_version": context["input_version"],
        }, {
            "provider": "deepseek", "model": "deepseek-flash", "reasoning": "high",
            "request_id": "review_synthetic", "usage": {"input_tokens": 1},
            "usage_status": "reported", "reviewed_at": "2026-09-19T00:00:00+00:00",
        }


def config(tmp_path, **extra):
    return {
        "execution_id": "explore_test", "attempt": 2,
        "ledger": str(tmp_path / "computer-use-observations.json"),
        "browser": "chrome", "allow_hosts": ["127.0.0.1"],
        "host_allowlist": ["127.0.0.1"], "allow_browser_click": True,
        "allowed_browser_effects": ["local_ephemeral"],
        "browser_element_grants": [
            {"action": "click", "host": "127.0.0.1", "expected_text": "Details",
             "effect": "local_ephemeral"},
            {"action": "type", "host": "127.0.0.1", "expected_text": "Search",
             "effect": "local_ephemeral"},
        ],
        "allowed_apps": ["com.example.KinProbe"],
        "allowed_app_actions": ["observe", "click", "scroll"],
        "allowed_native_effects": ["local_ephemeral"],
        "native_element_grants": [
            {"action": "click", "app_id": "com.example.KinProbe", "expected_text": "Toggle",
             "effect": "local_ephemeral"},
        ],
        "action_review": {
            "allowed_categories": ["navigation", "local_reversible"],
            "local_write_hosts": [], "local_write_apps": [],
        },
        **extra,
    }


def test_browser_actions_are_run_owned_fixed_and_receipted(tmp_path):
    backend = FakeBackend(
        {"tab_id": "42", "url": "http://127.0.0.1:8765/", "title": "Probe",
         "state": "0 AXWebArea Probe\n  1 button Details"},
        {"tab_id": "42", "url": "http://127.0.0.1:8765/", "title": "Probe",
         "state": "0 AXWebArea Probe\n  1 button Details"},
        {"tab_id": "42", "url": "http://127.0.0.1:8765/", "title": "Probe",
         "state": "0 AXWebArea Probe\n  1 button Details"},
        {"tab_id": "42", "url": "http://127.0.0.1:8765/", "title": "Probe",
         "state": "0 AXWebArea Probe\n  1 button Details [active]\n  2 staticText Expanded"},
        {"closed": True, "tab_id": "42"},
    )
    reviewer = FakeReviewer()
    controller = ComputerUseController(config(tmp_path), backend, reviewer)

    opened = asyncio.run(controller.open_browser_page("http://127.0.0.1:8765/"))
    clicked = asyncio.run(controller.click_browser_element(
        "42", 1, "Details", "local_ephemeral"
    ))
    closed = asyncio.run(controller.close_browser_page("42"))

    assert opened["state"] == clicked["state"] == "observed"
    assert opened["adapter"] == "kin-computer-use-v1"
    assert valid_computer_receipt(opened, execution_id="explore_test", attempt=2)
    assert clicked["action"]["kind"] == "click"
    assert clicked["action"]["expected_text"] == "Details"
    assert clicked["action"]["declared_effect"] == "local_ephemeral"
    assert clicked["action"]["authorization"]["source"] == "deepseek-action-review"
    assert reviewer.requests[0]["candidate"]["host_grant_id"].startswith("grant_")
    assert closed["state"] == "closed"
    assert "createBrowserTab" in backend.calls[0][0]
    assert all("eval(" not in code and "require(" not in code for code, _title in backend.calls)
    stored = json.loads((tmp_path / "computer-use-observations.json").read_text())
    assert {value["state"] for value in stored.values()} == {"observed", "reviewed", "acted"}


def test_ui_effect_and_target_gates_fail_before_action_without_keyword_semantics(tmp_path):
    dangerous = FakeBackend()
    controller = ComputerUseController(config(tmp_path), dangerous)
    with pytest.raises(ValueError, match="ui-effect-invalid"):
        asyncio.run(controller.click_browser_element("42", 1, "Send", "bad effect"))
    assert dangerous.calls == []

    # DS cannot relabel Send as a local edit. Independent review classifies the
    # actual effect as external_send, which the host denies before the click.
    relabel_backend = FakeBackend(
        {"tab_id": "42", "url": "http://127.0.0.1:8765/", "title": "Probe",
         "state": "1 button Send"},
    )
    relabel_review = FakeReviewer(decision="allow", category="external_send")
    relabel = ComputerUseController(config(
        tmp_path / "relabel", allowed_browser_effects=["local_edit"],
        browser_element_grants=[],
    ), relabel_backend, relabel_review)
    with pytest.raises(ValueError, match="ui-action-review-denied:external_send"):
        asyncio.run(relabel.click_browser_element("42", 1, "Send", "local_edit"))
    assert len(relabel_backend.calls) == 1
    assert relabel_review.requests[0]["candidate"]["element_line"] == "1 button Send"
    stored = json.loads((tmp_path / "relabel" / "computer-use-observations.json").read_text())
    assert {value["state"] for value in stored.values()} == {"reviewed"}

    # Words are not policy. A local Save control is allowed when DS declares an
    # effect that this run explicitly grants and the current AX identity matches.
    save_backend = FakeBackend(
        {"tab_id": "42", "url": "http://127.0.0.1:8765/", "title": "Probe",
         "state": "1 button Save"},
        {"tab_id": "42", "url": "http://127.0.0.1:8765/", "title": "Probe",
         "state": "1 button Save"},
        {"tab_id": "42", "url": "http://127.0.0.1:8765/", "title": "Probe",
         "state": "1 button Save [active]"},
    )
    save_review = FakeReviewer()
    save = ComputerUseController(config(
        tmp_path / "save", allowed_browser_effects=["local_edit"],
        browser_element_grants=[
            {"action": "click", "host": "127.0.0.1", "expected_text": "Save",
             "effect": "local_edit"},
        ],
    ), save_backend, save_review)
    assert asyncio.run(save.click_browser_element(
        "42", 1, "Save", "local_edit"
    ))["state"] == "observed"
    assert save_review.requests[0]["candidate"]["host_grant_id"].startswith("grant_")

    grant_only_backend = FakeBackend(
        {"tab_id": "42", "url": "http://127.0.0.1:8765/", "title": "Probe",
         "state": "1 button Save"},
    )
    grant_only = ComputerUseController(config(
        tmp_path / "grant-only", allowed_browser_effects=["local_edit"],
        browser_element_grants=[
            {"action": "click", "host": "127.0.0.1", "expected_text": "Save",
             "effect": "local_edit"},
        ],
    ), grant_only_backend)
    with pytest.raises(ValueError, match="ui-action-review-unavailable"):
        asyncio.run(grant_only.click_browser_element("42", 1, "Save", "local_edit"))
    assert all(".click(1)" not in code for code, _title in grant_only_backend.calls)

    # A short model label and matching grant cannot authorize a longer, more
    # consequential real AX label, and no reviewer/action runs after mismatch.
    deceptive_backend = FakeBackend(
        {"tab_id": "42", "url": "http://127.0.0.1:8765/", "title": "Probe",
         "state": "1 button Save and send externally"},
    )
    deceptive_review = FakeReviewer()
    deceptive = ComputerUseController(config(
        tmp_path / "deceptive", allowed_browser_effects=["local_edit"],
        browser_element_grants=[
            {"action": "click", "host": "127.0.0.1", "expected_text": "Save",
             "effect": "local_edit"},
        ],
    ), deceptive_backend, deceptive_review)
    with pytest.raises(ValueError, match="accessibility-element-changed"):
        asyncio.run(deceptive.click_browser_element("42", 1, "Save", "local_edit"))
    assert deceptive_review.requests == []
    assert all(".click(1)" not in code for code, _title in deceptive_backend.calls)

    with pytest.raises(ValueError, match="native-app-not-authorized"):
        asyncio.run(controller.observe_native_app("com.tencent.xinWeChat"))
    with pytest.raises(ValueError, match="browser-host-not-allowlisted"):
        asyncio.run(controller.open_browser_page("http://169.254.169.254/latest/meta-data"))
    with pytest.raises(ValueError, match="browser-sensitive-query-refused"):
        asyncio.run(controller.open_browser_page("https://example.com/?token=private"))


def test_unknown_public_control_uses_independent_review_and_fresh_snapshot(tmp_path):
    state = "0 AXWebArea Public page\n  7 button Expand new section"
    backend = FakeBackend(
        {"tab_id": "42", "url": "http://127.0.0.1:8765/", "title": "Public", "state": state},
        {"tab_id": "42", "url": "http://127.0.0.1:8765/", "title": "Public", "state": state},
        {"tab_id": "42", "url": "http://127.0.0.1:8765/", "title": "Public",
         "state": state + "\n  8 staticText Expanded"},
    )
    reviewer = FakeReviewer(category="local_reversible")
    controller = ComputerUseController(config(
        tmp_path, host_allowlist=[], allowed_browser_effects=[], browser_element_grants=[],
    ), backend, reviewer)
    receipt = asyncio.run(controller.click_browser_element(
        "42", 7, "Expand new section", "page_state_change"
    ))
    authorization = receipt["action"]["authorization"]
    assert authorization["source"] == "deepseek-action-review"
    assert authorization["category"] == "local_reversible"
    assert reviewer.requests[0]["snapshot_hash"] == authorization["snapshot_hash"]
    assert ".click(7)" in backend.calls[-1][0]


def test_review_binding_and_snapshot_fence_fail_closed(tmp_path, monkeypatch):
    context = {"snapshot_hash": "a" * 64, "input_version": "b" * 64,
               "candidate": {"action": "click"}}

    def factory(timeout):
        async def answer(request):
            assert request.headers["Authorization"] == "Bearer ephemeral-review-token"
            decision = {
                "decision": "allow", "category": "local_reversible",
                "effect": "expand local section", "target": "button",
                "reason": "reversible page state", "snapshot_hash": "a" * 64,
                "input_version": "b" * 64,
            }
            return httpx.Response(200, json={
                "id": "review_real_shape", "model": "deepseek-flash", "status": "completed",
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "output": [{"type": "message", "content": [
                    {"type": "output_text", "text": json.dumps(decision)},
                ]}],
            })
        return httpx.AsyncClient(transport=httpx.MockTransport(answer), timeout=timeout)

    monkeypatch.setenv("KIN_TEST_ACTION_REVIEW", "ephemeral-review-token")
    reviewer = DeepSeekActionReviewer({
        "base_url": "http://127.0.0.1:3456/v1", "env_key": "KIN_TEST_ACTION_REVIEW",
        "model": "deepseek-flash", "reasoning": "high", "timeout_seconds": 5,
    }, client_factory=factory)
    decision, receipt = asyncio.run(reviewer.review(context))
    assert decision["category"] == "local_reversible"
    assert receipt["request_id"] == "review_real_shape" and receipt["reasoning"] == "high"

    before = "0 AXWebArea Public\n  7 button Expand"
    changed = before + "\n  8 staticText Changed while reviewing"
    backend = FakeBackend(
        {"tab_id": "42", "url": "http://127.0.0.1:8765/", "title": "Public", "state": before},
        {"tab_id": "42", "url": "http://127.0.0.1:8765/", "title": "Public", "state": changed},
    )
    controller = ComputerUseController(config(
        tmp_path, allowed_browser_effects=[], browser_element_grants=[],
    ), backend, FakeReviewer())
    with pytest.raises(ValueError, match="ui-snapshot-changed-after-review"):
        asyncio.run(controller.click_browser_element("42", 7, "Expand", "page_state_change"))
    assert all(".click(7)" not in code for code, _title in backend.calls)

    # Changes beyond the bounded review text are still fenced by a hash of the
    # complete raw AX state. The exact target line is included in review context.
    prefix = "0 AXWebArea Public\n" + ("x" * (MAX_STATE + 20)) + "\n  7 button Save\n"
    before = prefix + "  8 staticText Before"
    changed = prefix + "  8 staticText After"
    tail_backend = FakeBackend(
        {"tab_id": "42", "url": "http://127.0.0.1:8765/", "title": "Public",
         "state": before},
        {"tab_id": "42", "url": "http://127.0.0.1:8765/", "title": "Public",
         "state": changed},
    )
    tail_review = FakeReviewer()
    tail = ComputerUseController(config(
        tmp_path / "tail", allowed_browser_effects=[], browser_element_grants=[],
    ), tail_backend, tail_review)
    with pytest.raises(ValueError, match="ui-snapshot-changed-after-review"):
        asyncio.run(tail.click_browser_element("42", 7, "Save", "page_state_change"))
    assert "7 button Save" in tail_review.requests[0]["snapshot"]
    assert len(tail_review.requests[0]["snapshot"]) <= MAX_STATE
    assert all(".click(7)" not in code for code, _title in tail_backend.calls)


def test_text_is_bounded_non_sensitive_and_json_encoded(tmp_path):
    state = "0 AXWebArea Probe\n  3 textField Search"
    backend = FakeBackend(
        {"tab_id": "42", "url": "http://127.0.0.1:8765/", "title": "Probe", "state": state},
        {"tab_id": "42", "url": "http://127.0.0.1:8765/", "title": "Probe", "state": state},
        {"tab_id": "42", "url": "http://127.0.0.1:8765/", "title": "Probe", "state": state},
    )
    controller = ComputerUseController(
        config(tmp_path, allow_browser_text=True), backend, FakeReviewer()
    )
    value = 'safe query "; globalThis.pwned=true; //'
    receipt = asyncio.run(controller.type_browser_text(
        "42", 3, "Search", value, "local_ephemeral"
    ))
    action_code = backend.calls[-1][0]
    assert json.dumps(value, ensure_ascii=False, separators=(",", ":")) in action_code
    assert receipt["action"]["characters"] == len(value) and "text" not in receipt["action"]

    # A path is ordinary operation data, not a credential by spelling alone.
    # Its target/effect still requires independent action review; an exact host
    # grant is only a hint, and the raw value is never copied into the receipt.
    for number, local_path in enumerate((
        "/Users/ica/Documents/notes.txt", r"C:\Users\ica\Documents\notes.txt",
    )):
        path_backend = FakeBackend(
            {"tab_id": "42", "url": "http://127.0.0.1:8765/", "title": "Probe",
             "state": state},
            {"tab_id": "42", "url": "http://127.0.0.1:8765/", "title": "Probe",
             "state": state},
            {"tab_id": "42", "url": "http://127.0.0.1:8765/", "title": "Probe",
             "state": state},
        )
        path_controller = ComputerUseController(
            config(tmp_path / f"path-{number}", allow_browser_text=True), path_backend,
            FakeReviewer(),
        )
        path_receipt = asyncio.run(path_controller.type_browser_text(
            "42", 3, "Search", local_path, "local_ephemeral"
        ))
        assert json.dumps(local_path, ensure_ascii=False, separators=(",", ":")) \
            in path_backend.calls[-1][0]
        assert "text" not in path_receipt["action"]

    refused = ComputerUseController(config(tmp_path / "off"), FakeBackend())
    with pytest.raises(ValueError, match="browser-text-entry-disabled"):
        asyncio.run(refused.type_browser_text("42", 3, "Search", "hello", "local_ephemeral"))
    enabled = ComputerUseController(config(tmp_path / "secret", allow_browser_text=True), FakeBackend())
    with pytest.raises(ValueError, match="browser-text-refused"):
        asyncio.run(enabled.type_browser_text(
            "42", 3, "Search", "api_key=sk-secret_123456789", "local_ephemeral"
        ))


def test_native_app_observation_and_reversible_click_receipts(tmp_path):
    backend = FakeBackend(
        {"state": "0 AXApplication Kin Probe\n  4 button Toggle"},
        {"state": "0 AXApplication Kin Probe\n  4 button Toggle"},
        {"state": "0 AXApplication Kin Probe\n  4 button Toggle\n  5 staticText On"},
    )
    controller = ComputerUseController(config(tmp_path), backend, FakeReviewer())
    receipt = asyncio.run(controller.click_native_element(
        "com.example.KinProbe", 4, "Toggle", "local_ephemeral"
    ))
    assert receipt["locator"] == "computer://app/com.example.KinProbe"
    assert receipt["tool"] == "click_native_element" and receipt["action"]["reversible"] is True
    assert valid_computer_receipt(receipt, execution_id="explore_test", attempt=2)
    assert "getApp" in backend.calls[0][0] and ".click(4)" in backend.calls[2][0]


def test_server_exposes_fixed_tools_and_backend_env_is_reference_only(tmp_path, monkeypatch):
    server = create_server({**config(tmp_path), "backend": {"command": "/bin/false"}})
    listed = {tool.name: tool for tool in server._tool_manager.list_tools()}
    assert set(listed) == {
        "open_browser_page", "read_browser_page", "navigate_browser_page",
        "click_browser_element", "type_browser_text", "close_browser_page",
        "observe_native_app", "click_native_element", "scroll_native_app",
    }
    for name in (
            "click_browser_element", "type_browser_text",
            "click_native_element", "scroll_native_app"):
        assert "Copy ``expected_text`` exactly from the freshest AX line" \
            in listed[name].description
    with pytest.raises(ValueError, match="inline-env-refused"):
        _backend_environment({"env": {"API_TOKEN": "private"}})
    with pytest.raises(ValueError, match="inline-env-refused"):
        _backend_environment({"env": {"FOO": "sk-secret_123456789"}})
    monkeypatch.setenv("KIN_CUA_TEST_SURFACE", "browser,computer")
    assert _backend_environment({"env_vars": ["KIN_CUA_TEST_SURFACE"]})[
        "KIN_CUA_TEST_SURFACE"
    ] == "browser,computer"


def test_nested_cua_uses_one_host_scope_for_bootstrap_actions_and_cleanup():
    calls = []

    class Session:
        async def call_tool(self, name, arguments, meta=None):
            calls.append((name, arguments, meta))
            return SimpleNamespace(isError=False, content=[])

    backend = CuaBackend(
        {"command": "/bin/false", "args": []}, execution_id="explore_42", attempt=3,
    )
    backend.session = Session()
    asyncio.run(backend._call("void 0", "Synthetic operation"))
    asyncio.run(backend.__aexit__(None, None, None))
    scope = json.loads(backend.scope_meta["x-codex-turn-metadata"])
    assert scope == {
        "session_id": "kin-exploration:explore_42",
        "turn_id": "kin-exploration:explore_42:attempt-3",
        "model": "deepseek-flash",
    }
    assert calls[0][2] == calls[1][2] == backend.scope_meta
    assert calls[1][0] == "turn_ended"
    assert calls[1][1]["session_id"] == scope["session_id"]
    assert calls[1][1]["turn_id"] == scope["turn_id"]
    assert "call_id" not in scope and "authorization" not in scope


def test_nested_cua_failure_is_stable_and_does_not_echo_backend_payload():
    class Session:
        async def call_tool(self, _name, _arguments, meta=None):
            assert meta
            return SimpleNamespace(
                isError=True,
                content=[SimpleNamespace(
                    text="Permission denied for https://example.test/?token=private-value",
                )],
            )

    backend = CuaBackend(
        {"command": "/bin/false", "args": []}, execution_id="failed-call", attempt=1,
    )
    backend.session = Session()
    with pytest.raises(RuntimeError, match="^computer-use-service-permission-denied$") as failed:
        asyncio.run(backend._call("void 0", "Synthetic operation"))
    assert "private-value" not in str(failed.value)
    assert "example.test" not in str(failed.value)


def test_backend_initialization_failure_closes_its_stdio_stack():
    backend = CuaBackend(
        {"command": "/bin/false", "args": []}, execution_id="failed-probe", attempt=1,
    )
    with pytest.raises(Exception):
        asyncio.run(backend.__aenter__())
    assert backend.stack is None and backend.session is None


def test_source_ledger_rejects_bare_history_stale_continuation_and_fake_computer():
    url = "https://example.com/verified"
    bare_topic = {"previous_explorations": [{
        "id": "explore_old", "state": "complete",
        "result": {"sources": [{"url": url, "title": "Bare URL"}]},
    }]}
    assert build_ledger(bare_topic) == []

    receipt = seal_source_receipt({
        "state": "observed", "locator": url, "evidence_id": "web_" + "1" * 32,
        "version": "a" * 64, "title": "Verified", "basis": "read_page",
        "recorded_at": "2026-09-19T00:00:00+00:00", "execution_id": "explore_old",
        "attempt": 1, "tool": "read_page", "adapter": "kin-web-reader-v1",
    }, execution_id="explore_old", attempt=1)
    sealed_topic = {"previous_explorations": [{
        "id": "explore_old", "state": "complete",
        "result": {"sources": [{"url": url, "title": "Verified", "receipt": receipt}]},
    }]}
    assert build_ledger(sealed_topic)[0]["state"] == "historical"

    continuation = {"exploration_id": "explore_old", "attempt": 1, "sources_used": [
        {"state": "observed", "locator": url, "evidence_id": "web_1", "version": "a" * 64},
        {"state": "historical", "locator": "memory://s1", "evidence_id": "s1", "version": None},
    ]}
    valid, dropped = validate_continuation_sources(
        continuation, {"known_evidence": [{"source_id": "s1", "revision": 3}]},
    )
    assert valid == []
    assert {item["reason"] for item in dropped} == {
        "host-receipt-missing-or-invalid", "superseded-or-absent",
    }

    fake = {"state": "observed", "locator": "computer://current-context",
            "id": "computer_fake", "version": "a" * 64, "observed_at": "now"}
    assert build_ledger({}, computer_observations=[fake], execution_id="run", attempt=1) == []
