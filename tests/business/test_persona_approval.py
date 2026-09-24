"""The hard rule on the Python side: the persona canon the mind loads is refused when it asks no
owner confirmation, names no approval, or its texts no longer match its hashes; nothing the mind
proposes may change a trait the approved canon does not list as mutable; and a canon is put in
place only as the owner's external approval record names it, with all four of its fields. The one
Python path that puts a canon in place is a restore, reached from the library, the CLI and the
HTTP service; each is held to the record here. The rehearsal checks the record in place
(kin_mind.deploy_checks, test_deploy_checks.py) and the host's instruction path checks it too
(persona-approval.test.mjs). Synthetic roles only."""
import hashlib
import io
import json
from contextlib import redirect_stdout
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from eventmem.core import Engine
from eventmem.core.api import create_app
from eventmem.core.cli import main as cli
from eventmem.core.models import Scope
from eventmem.core.persona import approval_problems, approve_canon, load_persona, validate_trait_changes
from eventmem.core.transfer import backup, restore

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


def canon_of(**change):
    texts = {k: change.pop(k, v) for k, v in TEXTS.items()}
    return {"schema": 1, "version": "persona-v3", "scope": SCOPE.model_dump(), "requires_owner_confirmation": True,
            "approved_source": "src_" + "a" * 32, "mutable_trait_keys": ["interests"], **texts,
            **{k + "_sha256": hashlib.sha256(v.encode()).hexdigest() for k, v in texts.items()}, **change}


def record_of(policy):
    """What the owner's confirmation of this canon issues: the host's mind-config `persona_contract`."""
    return {field: policy[field] for field in ("version", "core_sha256", "voice_sha256", "maintenance_sha256")}


EDITS = {"voice": TEXTS["voice"] + "未经确认的改动。", "maintenance": TEXTS["maintenance"] + "未经确认的改动。"}


def test_a_record_with_only_the_core_hash_approves_no_canon_at_all():
    approved = record_of(canon_of())
    core_only = {"version": approved["version"], "core_sha256": approved["core_sha256"]}
    assert approval_problems(core_only) == ["approval-record-incomplete:voice_sha256",
                                            "approval-record-incomplete:maintenance_sha256"]
    # Not an edit of either part it leaves out, and not the canon it was taken from either.
    for policy in (canon_of(), canon_of(voice=EDITS["voice"]), canon_of(maintenance=EDITS["maintenance"])):
        with pytest.raises(ValueError, match=r"approval record is incomplete .*voice_sha256.*maintenance_sha256.*needs host review"):
            approve_canon(policy, core_only)
    for gap in ({"version": " "}, {"core_sha256": "0" * 63}, {"voice_sha256": "X" * 64}, {"maintenance_sha256": None}):
        with pytest.raises(ValueError, match="approval record is incomplete"):
            approve_canon(canon_of(), {**approved, **gap})
    with pytest.raises(ValueError, match=r"approval record is missing \(approval-record-missing\)"):
        approve_canon(canon_of(), None)
    assert approval_problems(approved) == []


def test_a_canon_goes_in_only_as_the_record_issued_for_its_text_names_it():
    approved = record_of(canon_of())
    assert approve_canon(canon_of(), approved)["version"] == "persona-v3"
    for part, text in EDITS.items():
        edited = canon_of(**{part: text})  # its own hashes rewritten to match
        with pytest.raises(ValueError, match=f"differs from the approved record \\(approval-record-differs:{part}_sha256\\)"):
            approve_canon(edited, approved)
        assert approve_canon(edited, record_of(edited))[part] == text
    with pytest.raises(ValueError, match="needs host review"):
        approve_canon({**canon_of(), "voice_sha256": "0" * 64}, record_of(canon_of()))


def store_with(tmp_path, name, policy):
    engine = Engine(tmp_path / name)
    (engine.db.root / "persona-policy.json").write_text(json.dumps(policy, ensure_ascii=False))
    archive = tmp_path / f"{name}.tar.gz"
    backup(engine, archive)
    return engine, archive


def test_a_restore_puts_a_canon_in_place_only_as_the_record_names_it(tmp_path):
    """The restore is the one Python path that puts a canon in place: a store restored from a
    backup runs with the backup's canon."""
    edited = canon_of(voice=EDITS["voice"])
    _, archive = store_with(tmp_path, "edited", edited)
    old = record_of(canon_of())
    refused = {"no record": (None, "approval record is missing"),
               "only the core hash": ({"version": old["version"], "core_sha256": old["core_sha256"]}, "approval record is incomplete"),
               "the record of the text before": (old, r"differs from the approved record \(approval-record-differs:voice_sha256\)")}
    for label, (approved, reason) in refused.items():
        target = tmp_path / ("refused-" + label.replace(" ", "-"))
        with pytest.raises(ValueError, match=reason):
            restore(archive, target, persona_approval=approved)
        assert not target.exists(), label
        assert not [p for p in tmp_path.iterdir() if p.name.startswith(".memorypalace-restore-")], "no staging is left"
    restored = restore(archive, tmp_path / "approved", persona_approval=record_of(edited))
    assert restored["status"] == "restored" and restored["persona_version"] == "persona-v3"
    assert json.loads((tmp_path / "approved" / "persona-policy.json").read_text()) == edited
    # A backup without a canon puts none in place: nothing to hold to a record.
    plain = tmp_path / "plain.tar.gz"
    backup(Engine(tmp_path / "plain"), plain)
    assert restore(plain, tmp_path / "plain-restored")["persona_version"] is None


def test_the_cli_restore_reads_the_record_from_the_host_configuration(tmp_path):
    edited = canon_of(maintenance=EDITS["maintenance"])
    _, archive = store_with(tmp_path, "edited", edited)
    config = tmp_path / "mind-config.json"
    config.write_text(json.dumps({"persona_contract": {"path": "/synthetic/persona-policy.json", **record_of(canon_of())}}))
    with pytest.raises(ValueError, match="differs from the approved record"):
        cli(["restore", str(archive), "--root", str(tmp_path / "stale"), "--persona-approval", str(config)])
    with pytest.raises(ValueError, match="approval record is missing"):
        cli(["restore", str(archive), "--root", str(tmp_path / "none")])
    assert not (tmp_path / "stale").exists() and not (tmp_path / "none").exists()
    config.write_text(json.dumps({"persona_contract": record_of(edited)}))
    with redirect_stdout(io.StringIO()) as printed:
        assert cli(["restore", str(archive), "--root", str(tmp_path / "renewed"), "--persona-approval", str(config)]) == 0
    assert json.loads(printed.getvalue())["persona_version"] == "persona-v3"
    assert json.loads((tmp_path / "renewed" / "persona-policy.json").read_text()) == edited


def test_the_service_restore_holds_the_canon_to_the_record_the_host_has_when_asked(tmp_path):
    edited = canon_of(voice=EDITS["voice"])
    _, archive = store_with(tmp_path, "edited", edited)
    host = {"record": record_of(canon_of())}
    service = Engine(tmp_path / "service")
    client = TestClient(create_app(engine=service, token="synthetic-token", workers=False, mcp_enabled=False,
                                   persona_approval=lambda: host["record"]))
    post = lambda: client.post("/v1/maintenance/restore", headers={"Authorization": "Bearer synthetic-token"},
                               files={"file": ("backup.tar.gz", archive.read_bytes(), "application/gzip")})
    answer = post()
    assert answer.status_code == 422 and "differs from the approved record" in answer.json()["detail"]
    assert list((service.db.root / "restores").iterdir()) == [], "nothing is published and no staging is left"
    host["record"] = {"version": "persona-v3", "core_sha256": record_of(edited)["core_sha256"]}
    assert "approval record is incomplete" in post().json()["detail"]
    # The owner's confirmation renews the record; the running service holds the canon to that one.
    host["record"] = record_of(edited)
    answer = post()
    assert answer.status_code == 200 and answer.json()["persona_version"] == "persona-v3"
    restored = [p for p in (service.db.root / "restores").iterdir()]
    assert len(restored) == 1 and json.loads((restored[0] / "persona-policy.json").read_text()) == edited
    # A service given no record restores no backup that carries a canon.
    bare = TestClient(create_app(engine=Engine(tmp_path / "bare"), token="synthetic-token", workers=False, mcp_enabled=False))
    refused = bare.post("/v1/maintenance/restore", headers={"Authorization": "Bearer synthetic-token"},
                        files={"file": ("backup.tar.gz", archive.read_bytes(), "application/gzip")})
    assert refused.status_code == 422 and "approval record is missing" in refused.json()["detail"]
