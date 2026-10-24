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


def test_current_persona_is_readable_without_an_approval_record(tmp_path):
    config = canon(tmp_path, authority="model", approved_source="missing-old-approval")
    config.pop("persona_contract")
    answer = persona_approval(config)
    assert answer["ok"] and answer["problems"] == []
    assert answer["source"] == "prompts/persona"
    assert "合成" not in json.dumps(answer, ensure_ascii=False)


def test_editable_persona_is_the_deployment_source_and_stale_seed_hashes_do_not_freeze_it(tmp_path):
    from eventmem.core.prompts import PromptStore
    config = canon(tmp_path)
    root = tmp_path / "MemoryPalace"
    saved = PromptStore(root / "prompts").write("persona", "我可以改变自己的说话方式，也记得小光与一一。")
    answer = persona_approval(config)
    assert answer["ok"] and answer["prompt_version"] == saved["version"]
    path = root / "persona-policy.json"
    policy = json.loads(path.read_text())
    path.write_text(json.dumps({**policy, "voice": "改变过的自然语气。"}, ensure_ascii=False))
    assert persona_approval(config)["ok"]


def test_unreadable_persona_reports_an_actual_document_problem(tmp_path):
    config = canon(tmp_path)
    path = tmp_path / "MemoryPalace" / "persona-policy.json"
    path.write_text("invalid JSON")
    assert persona_approval(config)["problems"] == ["persona-document-unreadable"]
    path.unlink()
    assert persona_approval(config)["problems"] == ["persona-seed-missing-or-other-scope"]


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
