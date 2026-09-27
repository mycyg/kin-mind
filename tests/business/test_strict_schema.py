"""The strict form of a schema a fork is held to, and its answer read back (strict_schema.py).

Every fork assessment was refused whole by the model API after WS4 (2026-09-27: `invalid_json_schema`,
"Missing 'strength'"), because a fork's outputSchema is held in strict mode and the pydantic schema
is not strict. These tests hold the strict form to the rules strict mode states, for every schema a
fork is sent, and read answers back into what the pydantic models accept.
"""
import json
from typing import Any

import pytest
from pydantic import Field, StrictInt

from eventmem.core.models import Model
from kin_mind import appraisal as A
from kin_mind.context import Compression
from kin_mind.creation import CompletionReview
from kin_mind.exploration import Findings
from kin_mind.lifecycle import EventSummary
from kin_mind.revalidation import Revalidation
from kin_mind.strict_schema import STRICT_KEYWORDS, decode, strict_schema


def violations(schema):
    """What strict mode refuses in `schema`, each as (path, problem)."""
    found = []
    def walk(node, path):
        if not isinstance(node, dict):
            found.append((path, "not a schema object"))
            return
        extra = set(node) - STRICT_KEYWORDS
        if extra:
            found.append((path, "keywords " + ",".join(sorted(extra))))
        if "$ref" in node and len(node) > 1:
            found.append((path, "$ref with siblings"))
        if not node or not ({"type", "anyOf", "$ref", "enum", "const"} & set(node)):
            found.append((path, "schema of anything"))
        if node.get("type") == "object":
            properties = node.get("properties")
            if properties is None:
                found.append((path, "object without properties"))
            elif node.get("required") != list(properties):
                found.append((path, "not every property required"))
            if node.get("additionalProperties") is not False:
                found.append((path, "additionalProperties not false"))
        for key in ("properties", "$defs"):
            for name, child in (node.get(key) or {}).items():
                walk(child, path + (key, name))
        if "items" in node:
            walk(node["items"], path + ("items",))
        for index, branch in enumerate(node.get("anyOf") or ()):
            walk(branch, path + ("anyOf", index))
    walk(schema, ())
    if schema.get("type") != "object":
        found.append(((), "root is not an object"))
    return found


def fork_schemas():
    """Every schema the main-session review sends to a fork: the assessment in each lane and section
    set, the history assessment, and each model a native `structured` call takes."""
    sections = tuple(A.AUDIT_SECTIONS)
    yield from ((f"appraisal operational={op} sections={len(s)}", A.appraisal_schema(op, False, s, A.REVIEW_MAX_MINUTES))
                for op in (False, True) for s in (sections, sections[:2], ()))
    yield "history", A.appraisal_schema(False, True, (), A.REVIEW_MAX_MINUTES)
    for model in (A.Appraisal, A.HistoryAssessment, A.SessionAdvice, A.SharingReview, Compression,
                  CompletionReview, EventSummary, Findings, Revalidation):
        yield model.__name__, model.model_json_schema()


@pytest.mark.parametrize("name,schema", list(fork_schemas()), ids=lambda value: value if isinstance(value, str) else "")
def test_every_schema_a_fork_is_sent_is_strict(name, schema):
    before = json.dumps(schema, sort_keys=True)
    assert violations(strict_schema(schema)) == [], name
    # The pydantic schema it came from is untouched: DeepSeek's tools still take it as it was.
    assert json.dumps(schema, sort_keys=True) == before


def test_the_pydantic_assessment_schema_itself_is_not_strict():
    """The failure this guards against, as the API reported it: the pydantic schema, sent as is."""
    problems = violations(A.appraisal_schema(False, False, tuple(A.AUDIT_SECTIONS), A.REVIEW_MAX_MINUTES))
    assert any(problem == "not every property required" for _, problem in problems)


class Inner(Model):
    level: StrictInt = Field(ge=0, le=100, description="强度")
    note: str | None = None


class Sample(Model):
    reason: str = Field(min_length=1)
    values: dict[str, StrictInt] = Field(default_factory=dict, max_length=20)
    inner: dict[str, Inner] = Field(default_factory=dict)
    loose: dict[str, Any] = Field(default_factory=dict)
    minutes: StrictInt = Field(default=20, ge=10, le=1440)
    maybe: str | None = None
    tags: list[str] = Field(default_factory=list, max_length=3)
    child: Inner | None = None
    kind: str = Field(default="a", pattern="^[a-z]$")


def test_bounds_become_words_and_maps_become_entries():
    strict = strict_schema(Sample.model_json_schema())
    assert violations(strict) == []
    properties = strict["properties"]
    assert properties["values"]["anyOf"][0]["type"] == "array"
    entry = properties["values"]["anyOf"][0]["items"]
    assert entry["required"] == ["key", "value"] and entry["properties"]["value"]["type"] == "integer"
    assert properties["loose"]["anyOf"][0]["items"]["properties"]["value"]["type"] == "string"
    assert properties["minutes"]["type"] == ["integer", "null"]
    assert "不小于 10" in properties["minutes"]["description"] and "不大于 1440" in properties["minutes"]["description"]
    assert "至多 3 项" in properties["tags"]["anyOf"][0]["description"]
    assert "符合正则" in properties["kind"]["description"]
    level = strict["$defs"]["Inner"]["properties"]["level"]
    assert level == {"type": "integer", "description": "强度 （不小于 0；不大于 100）"}


def test_an_answer_is_read_back_into_what_the_model_accepts():
    """Entries become the dict again, a null the model could have left out is left out (the default
    applies), a null the field takes stays, JSON text is read, and nested references are followed."""
    schema = Sample.model_json_schema()
    answer = {"reason": "测试", "values": [{"key": "curiosity", "value": 71}, {"key": "mood", "value": 60}],
              "inner": [{"key": "rest", "value": {"level": 40, "note": None}}],
              "loose": [{"key": "tone", "value": "{\"warm\": true}"}, {"key": "size", "value": "3"}],
              "minutes": None, "maybe": None, "tags": None, "child": {"level": 5, "note": "x"}, "kind": None}
    decoded = decode(schema, answer)
    assert decoded == {"reason": "测试", "values": {"curiosity": 71, "mood": 60},
                       "inner": {"rest": {"level": 40, "note": None}}, "loose": {"tone": {"warm": True}, "size": 3},
                       "maybe": None, "child": {"level": 5, "note": "x"}}
    model = Sample.model_validate(decoded)
    assert model.minutes == 20 and model.tags == [] and model.kind == "a" and model.inner["rest"].level == 40


def test_an_answer_in_the_original_shape_passes_unchanged():
    """The legacy channel pastes the schema into its prompt; a dict answered as a dict stays one."""
    schema = Sample.model_json_schema()
    answer = {"reason": "测试", "values": {"curiosity": 83}, "inner": {"rest": {"level": 1}}, "loose": {"tone": "warm"},
              "minutes": 30, "tags": ["a"]}
    assert decode(schema, answer) == answer
    assert decode(A.appraisal_schema(False, False, (), A.REVIEW_MAX_MINUTES), {"reason": "x", "values": {"mood": 70}}) == \
        {"reason": "x", "values": {"mood": 70}}


def test_a_strict_answer_to_the_assessment_validates():
    """The assessment itself, answered the way strict mode makes a model answer: every property
    present, the unused ones null, the dicts as entries."""
    schema = A.appraisal_schema(False, False, tuple(A.AUDIT_SECTIONS), A.REVIEW_MAX_MINUTES)
    answer = {name: None for name in strict_schema(schema)["properties"]}
    answer.update(reason="测试", values=[{"key": "curiosity", "value": 64}],
                  motivations=[{"key": "curiosity", "value": {"target": 70, "half_life_minutes": 60, "reason": "想知道"}}])
    appraisal = A.Appraisal.model_validate(decode(schema, answer))
    assert appraisal.values == {"curiosity": 64} and appraisal.motivations["curiosity"].target == 70
    assert appraisal.next_review_minutes == 20 and appraisal.wishes == []


@pytest.mark.parametrize("keys", [("mood", "mood"), ([],), (1,)])
def test_map_entries_cannot_silently_overwrite_or_use_non_string_keys(keys):
    schema = Sample.model_json_schema()
    with pytest.raises(ValueError, match="strict-map-key-invalid-or-duplicate"):
        decode(schema, {"values": [{"key": key, "value": 50} for key in keys]})


def test_optional_any_keeps_explicit_null_instead_of_applying_default():
    class Arbitrary(Model):
        value: Any = "default"
    schema = Arbitrary.model_json_schema()
    assert decode(schema, {"value": None}) == {"value": None}
    assert Arbitrary.model_validate(decode(schema, {"value": None})).value is None


def test_required_null_and_scalar_union_are_left_for_pydantic_validation():
    from pydantic import ValidationError
    class Mixed(Model):
        path: list[str | StrictInt]
        count: StrictInt = 3
        nullable: str | None = "default"
    schema = Mixed.model_json_schema()
    result = decode(schema, {"path": ["3", 3], "count": None, "nullable": None})
    assert result == {"path": ["3", 3], "nullable": None}
    assert Mixed.model_validate(result).count == 3
    with pytest.raises(ValidationError):
        Mixed.model_validate(decode(schema, {"path": None}))


def test_pydantic_still_rejects_bounds_after_decoding():
    from pydantic import ValidationError
    for answer in ({"reason": ""}, {"reason": "x", "minutes": 1},
                   {"reason": "x", "tags": ["a"] * 4}):
        with pytest.raises(ValidationError):
            Sample.model_validate(decode(Sample.model_json_schema(), answer))
