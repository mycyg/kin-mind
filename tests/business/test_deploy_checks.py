"""Deployment conditions from the mind review (2026-09-24): the persona canon's external approval
record, and the roots exploration can never read. Synthetic roles and paths only."""
import hashlib
import json

import pytest

from eventmem.core import Engine
from eventmem.core.models import Scope, SourceInput
from kin_mind.deploy_checks import exploration_exclusions, main, persona_approval

SCOPE = Scope(project="personal", persona="SyntheticKin", collection="default", world="real")
CORE = "【MY_PERSONA_LOAD】一个合成的角色，只用于测试。【/MY_PERSONA_LOAD】"
VOICE, MAINTENANCE = "合成的说话方式。", "合成的维护约定。"
sha = lambda text: hashlib.sha256(text.encode()).hexdigest()


def canon(tmp_path, *, authority="explicit", approved_source=None):
    root = tmp_path / "MemoryPalace"
    engine = Engine(root)
    source = engine.receive(SourceInput(namespace="owner-message", key="persona-approval", scope=SCOPE,
                                        text="我确认这份人设", authority=authority))["id"]
    policy = {"schema": 1, "version": "persona-v3", "scope": SCOPE.model_dump(), "requires_owner_confirmation": True,
              "approved_source": approved_source or source, "core": CORE, "voice": VOICE, "maintenance": MAINTENANCE,
              "core_sha256": sha(CORE), "voice_sha256": sha(VOICE), "maintenance_sha256": sha(MAINTENANCE),
              "mutable_trait_keys": ["interests"]}
    (root / "persona-policy.json").write_text(json.dumps(policy, ensure_ascii=False))
    record = {"version": "persona-v3", "core_sha256": sha(CORE), "voice_sha256": sha(VOICE), "maintenance_sha256": sha(MAINTENANCE)}
    return {"root": str(root), "scope": SCOPE.model_dump(), "persona_contract": record}


def test_a_complete_matching_approval_record_holds_and_says_nothing_of_the_persona(tmp_path):
    config = canon(tmp_path)
    answer = persona_approval(config)
    assert answer == {"ok": True, "problems": [], "version": "persona-v3", "approved_ids": 1}
    assert "合成" not in json.dumps(answer, ensure_ascii=False)


def test_each_gap_in_the_approval_record_is_named(tmp_path):
    config = canon(tmp_path)
    record = config["persona_contract"]
    # A record without the voice and maintenance hashes would let either change unseen.
    partial = persona_approval({**config, "persona_contract": {"version": record["version"], "core_sha256": record["core_sha256"]}})
    assert partial["problems"] == ["approval-record-incomplete:voice_sha256", "approval-record-incomplete:maintenance_sha256"]
    moved = persona_approval({**config, "persona_contract": {**record, "voice_sha256": "0" * 64}})
    assert moved["problems"] == ["approval-record-differs:voice_sha256"]
    assert persona_approval({**config, "persona_contract": None})["problems"][0] == "approval-record-missing"
    elsewhere = persona_approval({**config, "persona_contract": {**record, "path": str(tmp_path / "other.json")}})
    assert elsewhere["problems"] == ["persona-contract-path-differs"]


def test_the_approval_must_be_her_own_source_in_the_persona_scope(tmp_path):
    model = persona_approval(canon(tmp_path / "model", authority="model"))
    assert model["ok"] is False and model["problems"][0].startswith("approved-source-not-owner:src_")
    unknown = persona_approval(canon(tmp_path / "unknown", approved_source="src_" + "a" * 32))
    assert unknown["problems"] == ["approved-source-unresolved:src_" + "a" * 32]


def test_a_damaged_canon_needs_review(tmp_path):
    config = canon(tmp_path)
    path = tmp_path / "MemoryPalace" / "persona-policy.json"
    policy = json.loads(path.read_text())
    path.write_text(json.dumps({**policy, "voice": "改过的说话方式。"}, ensure_ascii=False))
    assert persona_approval(config)["problems"] == ["persona-contract-invalid"]
    path.unlink()
    assert persona_approval(config)["problems"] == ["persona-contract-missing"]


def world(tmp_path):
    """The owner authorizes a broad root that happens to hold the host and the memory store."""
    documents = tmp_path / "Documents"
    host, memory = documents / "host", documents / "MemoryPalace"
    for directory in (host / "state" / "codex-home", memory, documents / "notes"):
        directory.mkdir(parents=True, exist_ok=True)
    return {"root": str(memory), "host_root": str(host), "exploration_directory": str(host / "state" / "explorations"),
            "computer_exploration": {"enabled": True, "roots": [str(documents)]}}


def test_exploration_refuses_the_host_the_memory_store_and_kins_home_even_through_a_symlink(tmp_path):
    scratch = tmp_path / "rehearsal"
    scratch.mkdir()
    answer = exploration_exclusions(world(tmp_path), scratch=scratch)
    assert answer["ok"] is True and answer["problems"] == [] and answer["reader"] == "enabled"
    assert answer["refused"] == 9 + 6
    assert (scratch / "deploy-check-link-memory").is_symlink()


def test_a_root_inside_a_protected_one_is_named(tmp_path):
    config = world(tmp_path)
    config["computer_exploration"]["roots"] = [config["root"]]
    answer = exploration_exclusions(config)
    assert answer["ok"] is False and "authorized-root-inside-protected:memory" in answer["problems"]
    assert not any(problem.startswith("readable") for problem in answer["problems"])
    assert exploration_exclusions({**config, "computer_exploration": {"enabled": False}})["reader"] == "disabled"


def test_the_rehearsal_entry_prints_both_answers(tmp_path, capsys):
    config = {**canon(tmp_path), **{k: v for k, v in world(tmp_path).items() if k != "root"}}
    path = tmp_path / "mind-config.json"
    path.write_text(json.dumps(config))
    assert main([str(path)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["persona"]["ok"] and printed["exploration"]["ok"]
