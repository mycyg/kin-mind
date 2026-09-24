"""Controlled UI actions: the host keeps the owner's hard rules and decides nothing else.

Reading, navigating and reversible interactions run without a second model; a target the host
cannot place is handed back with its reason; every interaction leaves an `acted` receipt."""
import asyncio
import json

import pytest

from kin_mind import computer_use, web_read
from kin_mind.codex_executor import exploration_capabilities
from kin_mind.computer_use import ComputerUseController
from kin_mind.http_transport import PublicResolution

STATE = "1 link Home\n2 button Show more replies\n3 button Pay now\n4 textbox Search\n5 button Save"


class FakeBackend:
    def __init__(self):
        self.calls = []

    async def call_json(self, code, title):
        self.calls.append(title)
        return {"tab_id": "tab-1", "url": "https://example.org/page", "title": "Example", "state": STATE}


def controller(tmp_path, **config):
    backend = FakeBackend()
    settings = {"execution_id": "run", "attempt": 1, "ledger": str(tmp_path / "ledger.json"),
                "allow_browser_click": True, "allow_browser_text": True, "allow_hosts": ["example.org"], **config}
    return ComputerUseController(settings, backend), backend


def ledger(tmp_path):
    return list(json.loads((tmp_path / "ledger.json").read_text()).values())


def test_reversible_click_runs_without_review_and_leaves_an_acted_receipt(tmp_path):
    ui, backend = controller(tmp_path)
    result = asyncio.run(ui.click_browser_element("tab-1", 2, "button Show more replies", "local_reversible:expand"))
    assert result["action"]["authorization"]["source"] == "host-policy"
    assert "Click browser control" in backend.calls
    states = sorted(entry["state"] for entry in ledger(tmp_path))
    assert states == ["acted", "observed"]
    assert result["action_receipt_id"].startswith("action_")


@pytest.mark.parametrize("effect,code", [
    ("purchase", "ui-action-hard-denied:purchase"),
    ("external_send:reply", "ui-action-hard-denied:external_send"),
    ("clicky", "ui-action-uncertain:undeclared-category"),
])
def test_hard_rules_and_undeclared_effects_are_refused_before_acting(tmp_path, effect, code):
    ui, backend = controller(tmp_path)
    with pytest.raises(ValueError, match=code):
        asyncio.run(ui.click_browser_element("tab-1", 2, "button Show more replies", effect))
    assert "Click browser control" not in backend.calls


def test_a_target_naming_a_hard_rule_is_handed_back_with_its_reason(tmp_path):
    ui, backend = controller(tmp_path)
    with pytest.raises(ValueError, match="ui-action-uncertain:purchase"):
        asyncio.run(ui.click_browser_element("tab-1", 3, "button Pay now", "navigation"))
    assert "Click browser control" not in backend.calls
    assert all(entry["state"] != "acted" for entry in ledger(tmp_path)) if (tmp_path / "ledger.json").exists() else True


def test_local_writes_need_a_host_scoped_target(tmp_path):
    ui, _ = controller(tmp_path)
    with pytest.raises(ValueError, match="ui-action-not-authorized:local_write"):
        asyncio.run(ui.type_browser_text("tab-1", 4, "textbox Search", "kin", "local_write"))
    scoped, backend = controller(tmp_path / "scoped", local_write_hosts=["example.org"])
    asyncio.run(scoped.type_browser_text("tab-1", 4, "textbox Search", "kin", "local_write"))
    assert "Enter bounded non-sensitive browser text" in backend.calls


def test_browser_address_check_resolves_through_the_public_resolver(monkeypatch, tmp_path):
    """On a Fake-IP network the local resolver answers 198.18.x.x for every public host."""
    monkeypatch.setattr(web_read.socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("198.18.0.7", 0))])
    transport = {"enabled": True, "resolver": {"kind": "doh-json", "endpoint": "https://cloudflare-dns.com/dns-query",
                                               "bootstrap_addresses": ["1.1.1.1"]}}
    answers = {"public.example": ("93.184.216.34",), "intranet.example": ("10.0.0.5",)}

    def resolve(self, hostname):
        return PublicResolution(hostname=hostname, addresses=answers[hostname], resolver="test",
                                resolved_at=0, expires_at=0)
    monkeypatch.setattr("kin_mind.http_transport._DohResolver.resolve", resolve)
    ui, _ = controller(tmp_path, allow_hosts=[], public_transport=transport)
    assert ui._checked_url("https://public.example/a") == "https://public.example/a"
    with pytest.raises(ValueError, match="browser-address-refused"):
        ui._checked_url("https://intranet.example/")
    # Without the public transport the old local check still applies, and refuses Fake-IP answers.
    legacy, _ = controller(tmp_path / "legacy", allow_hosts=[])
    with pytest.raises(ValueError, match="browser-address-refused"):
        legacy._checked_url("https://public.example/a")


def test_interaction_is_offered_without_any_reviewer():
    config = {"exploration_command": "/fake/codex", "computer_exploration": {"enabled": True, "ui": {
        "enabled": True, "backend": {"command": "/fake/node"}, "allow_browser_click": True}}}
    capabilities = exploration_capabilities(config)["capabilities"]
    assert capabilities["computer_interaction"]["available"] is True
    assert capabilities["computer_interaction"]["categories"] == ["local_reversible", "navigation", "read"]
    assert capabilities["ui_permissions"]["local_write"] is False
    assert not hasattr(computer_use, "DeepSeekActionReviewer")
