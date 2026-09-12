import hashlib
import json

import httpx
import pytest

from eventmem.core import Engine
from eventmem.core.models import Scope, ModelRole, SourceInput
from eventmem.core.persona import load_persona, validate_trait_changes
from eventmem.core.providers import Providers
from kin_mind.appraisal import DeepSeek
from kin_mind.state import Mind, AffectiveEvent, Evolution


@pytest.fixture
def configured(tmp_path):
    engine = Engine(tmp_path)
    scope = Scope(persona="Synthetic")
    policy = dict(schema=1, version="test-v1", scope=scope.model_dump(), approved_source="owner-request",
                  requires_owner_confirmation=True, core="【MY_PERSONA_LOAD】 SYNTHETIC\n【/MY_PERSONA_LOAD】",
                  voice="Use complete sentences.", maintenance="Preserve source quotes. Do not rewrite role configuration.",
                  mutable_trait_keys=["interests"])
    for key in ("core", "voice", "maintenance"):
        policy[key + "_sha256"] = hashlib.sha256(policy[key].encode()).hexdigest()
    (tmp_path / "persona-policy.json").write_text(json.dumps(policy))
    engine.settings("models", {key: ModelRole(endpoint="https://api.deepseek.com", model="synthetic", protocol="anthropic").model_dump()
                               for key in ("summary", "extraction", "conflict", "prediction")})
    return engine, scope, policy


def test_scope_and_hashes(configured):
    engine, scope, policy = configured
    assert load_persona(engine, scope)["version"] == "test-v1"
    assert load_persona(engine, Scope(persona="Other")) is None
    assert load_persona(engine, None) is None
    policy["voice"] = "Unauthorized rewrite"
    (engine.db.root / "persona-policy.json").write_text(json.dumps(policy))
    with pytest.raises(ValueError, match="host review"):
        load_persona(engine, scope)


@pytest.mark.parametrize("role", ["extraction", "conflict", "summary", "prediction"])
def test_memory_prompts_share_contract_without_changing_schema(configured, monkeypatch, role):
    engine, scope, policy = configured
    provider = Providers(engine)
    requests = []
    def request(*args, **kwargs):
        requests.append(kwargs["json_"])
        return {"content": [{"type": "text", "text": '{"content":"Observation."}'}]}
    monkeypatch.setattr(provider, "request", request)
    for current in (scope, Scope(persona="Other")):
        assert provider.json(role, "Return the required JSON schema.", {"scope": current.model_dump()}) == {"content": "Observation."}
    assert policy["core"] in requests[0]["system"]
    assert "Return the required JSON schema." in requests[0]["system"]
    assert "Preserve source quotes" in requests[0]["system"]
    assert policy["core"] not in requests[1]["system"]


@pytest.mark.parametrize("mode", ["interaction", "daily-personality-review"])
def test_affective_evaluator_reports_contract_version(configured, monkeypatch, mode):
    engine, scope, policy = configured
    monkeypatch.setenv("SYNTHETIC_PERSONA_TEST_KEY", "synthetic")
    requests = []
    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"model": "synthetic", "id": "test-receipt",
          "content": [{"type": "tool_use", "name": "submit_appraisal", "input": {"values": {}, "reason": "No new observation."}}]})
    provider = DeepSeek("https://api.deepseek.com", "synthetic", "SYNTHETIC_PERSONA_TEST_KEY", transport=httpx.MockTransport(respond))
    provider.engine = engine
    _, receipt = provider.appraise({"mode": mode, "state": {"scope": scope.model_dump()}})
    assert policy["core"] in requests[0]["system"]
    assert receipt["persona_contract"]["version"] == "test-v1"


def test_state_and_evolution_respect_frozen_core(configured):
    engine, scope, policy = configured
    mind = Mind(engine, scope)
    source = engine.receive(SourceInput(namespace="test", key="role", scope=scope, text="Synthetic role"))
    mind.initialize(agent_version="test-v1", evidence_ids=[source["id"]])
    assert mind.read()["persona_contract"]["core_sha256"] == policy["core_sha256"]
    validate_trait_changes(policy, {"interests": "Astronomy"})
    request = AffectiveEvent(command_id="test", agent_version="test-v1", expected_revision=1, evidence_ids=["synthetic"], reason="test",
                            evolution=Evolution(claim_id="synthetic", assessment_id="synthetic", traits={"identity": "Different role"}))
    with pytest.raises(ValueError, match="owner approval"):
        mind._evolve(None, {}, request, [], "synthetic-event")
