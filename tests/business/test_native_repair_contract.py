"""A native repair sees the same wire contract as the original assessment."""
from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator

from kin_mind.appraisal import Appraisals
from kin_mind.strict_schema import decode
from test_kin_mind import setup
from test_main_session_review import native_provider


@pytest.mark.parametrize("fixed_minutes,expected", [(720, "complete"), (1440, "pending")])
def test_native_motivation_repair_keeps_wire_maps_and_effective_schema(setup, fixed_minutes, expected):
    """Regression: 2880 minutes, then repaired values returned as a dict.

    The isolated model edits only the reported number. The real native adapter,
    wire validator and Appraisal validator perform the rest of the round trip.
    """
    mind, source, clock = setup
    requests = []
    original = None

    def exchange(request):
        nonlocal original
        requests.append(deepcopy(request))
        receipt = {"native_turn_id": "turn-" + str(len(requests)), "model": "gpt-6-astra",
                   "usage": {"input_tokens": 500, "output_tokens": 100}}
        if request["name"] == "submit_appraisal":
            original = {key: None for key in request["schema"]["properties"]}
            original.update(reason="Review the local lifecycle requests", values=[{"key": "curiosity", "value": 74}],
                motivations=[{"key": "curiosity", "value": {"target": 74, "half_life_minutes": 2880,
                                                              "reason": "Curious about the results"}}])
            result = original
        else:
            assert request["name"] == "repair_appraisal" and len(requests) == 2
            assert request["schema"] == requests[0]["schema"]
            assert request["context"]["errors"][0]["loc"] == ["motivations", "curiosity", "half_life_minutes"]
            result = deepcopy(request["context"]["proposal"])
            assert result["values"] == original["values"]
            assert result["motivations"] == original["motivations"]
            result["motivations"][0]["value"]["half_life_minutes"] = fixed_minutes
        Draft202012Validator(request["schema"]).validate(result)
        return {"state": "complete", "result": result, "receipt": receipt}

    provider = native_provider(mind, exchange)
    jobs = Appraisals(mind)
    job = jobs.enqueue([source("local-lifecycle-request")], "synthetic-v1")
    result = jobs.run_one(provider, job_id=job["id"])
    assert result["state"] == expected, result
    assert [r["name"] for r in requests] == ["submit_appraisal", "repair_appraisal"]
    assert original["motivations"][0]["value"]["half_life_minutes"] == 2880
    if expected == "complete":
        assert mind.read()["dimensions"]["curiosity"]["value"] == 74
        assert result["receipt"]["schema_errors"][0]["loc"] == ["motivations", "curiosity", "half_life_minutes"]
    else:
        assert "less_than_equal" in result["error"]


def test_repair_wire_encoding_preserves_nested_values_and_original_invalid_number():
    from kin_mind.strict_schema import encode, strict_schema
    from test_strict_schema import Sample
    schema = Sample.model_json_schema()
    original = {"reason": "repair", "values": {"mood": 74}, "inner": {"rest": {"level": 2880}},
                "loose": {"note": "1", "payload": {"same": True}}, "child": {"level": 5}}
    before = deepcopy(original)
    encoded = encode(schema, original)
    Draft202012Validator(strict_schema(schema)).validate(encoded)
    assert encoded["values"] == [{"key": "mood", "value": 74}]
    assert encoded["inner"][0]["value"]["level"] == 2880
    assert decode(schema, encoded)["loose"] == original["loose"]
    assert Sample.model_validate(decode(schema, encode(schema, {**original, "inner": {}}))).values == {"mood": 74}
    assert original == before
