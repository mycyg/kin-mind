"""The hard rule on the Python side: the persona canon the mind loads is refused when it asks no
owner confirmation, names no approval, or its texts no longer match its hashes; and nothing the
mind proposes may change a trait the approved canon does not list as mutable. The external
approval record itself is checked by kin_mind.deploy_checks (test_deploy_checks.py) and by the
host's reader (persona-approval.test.mjs). Synthetic roles only."""
import hashlib
import json
from types import SimpleNamespace

import pytest

from eventmem.core.models import Scope
from eventmem.core.persona import load_persona, validate_trait_changes

SCOPE = Scope(project="personal", persona="SyntheticKin", collection="default", world="real")
CORE = "【MY_PERSONA_LOAD】合成角色。【/MY_PERSONA_LOAD】"
TEXTS = {"core": CORE, "voice": "合成的说话方式。", "maintenance": "合成的维护约定。"}


def write(root, **change):
    texts = {k: change.pop(k, v) for k, v in TEXTS.items()}
    policy = {"schema": 1, "version": "persona-v3", "scope": SCOPE.model_dump(), "requires_owner_confirmation": True,
              "approved_source": "src_" + "a" * 32, "mutable_trait_keys": ["interests"], **texts,
              **{k + "_sha256": hashlib.sha256(v.encode()).hexdigest() for k, v in texts.items()}, **change}
    (root / "persona-policy.json").write_text(json.dumps(policy, ensure_ascii=False))
    return SimpleNamespace(db=SimpleNamespace(root=root))


def test_the_approved_canon_loads_for_its_own_scope_only(tmp_path):
    engine = write(tmp_path)
    assert load_persona(engine, SCOPE)["version"] == "persona-v3"
    assert load_persona(engine, Scope(persona="SomeoneElse")) is None


@pytest.mark.parametrize("change", [{"requires_owner_confirmation": False}, {"approved_source": ""}, {"version": ""},
                                    {"voice_sha256": "0" * 64}, {"core": "没有标记的人设"}, {"schema": 2}])
def test_a_canon_that_cannot_show_confirmation_needs_host_review(tmp_path, change):
    engine = write(tmp_path, **change)
    with pytest.raises(ValueError, match="needs host review"):
        load_persona(engine, SCOPE)


def test_only_the_traits_the_owner_left_mutable_may_change(tmp_path):
    policy = load_persona(write(tmp_path), SCOPE)
    validate_trait_changes(policy, {"interests": ["猫"]})
    with pytest.raises(ValueError, match="explicit owner approval"):
        validate_trait_changes(policy, {"core_temperament": "冷淡"})
