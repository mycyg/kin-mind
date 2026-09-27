"""Optional, private, owner-approved persona contract shared by all consumers.

The host installs this file after explicit owner approval. Generated memories and
model proposals cannot install it. Hashes detect drift, not a hostile OS user.

The file's own hashes sit beside its texts, so whoever rewrites the texts can rewrite
them too: they prove no approval. The owner's approval is the record the host keeps
outside the file (mind-config `persona_contract`). Every Python path that puts a canon
in place holds it against that record first (`approve_canon`); the consumers below read
the canon that is in place.
"""
from __future__ import annotations

import hashlib
import json
import re

from .models import Scope

# What the owner's approval record names: the version and the hash of every part.
APPROVAL_FIELDS = ("version", "core_sha256", "voice_sha256", "maintenance_sha256")
_HEX64 = re.compile(r"[0-9a-f]{64}")


def _checked(policy):
    """The canon's own fields, markers and hashes; anything else needs host review."""
    try:
        if policy["schema"] != 1 or policy["requires_owner_confirmation"] is not True or not policy["version"] or not policy["approved_source"]:
            raise ValueError("Invalid approval declaration")
        for name in ("core", "voice", "maintenance"):
            value = policy[name]
            if not isinstance(value, str) or not 0 < len(value) <= 16000 or hashlib.sha256(value.encode()).hexdigest() != policy[name + "_sha256"]:
                raise ValueError("Invalid persona hash")
        if not policy["core"].startswith("【MY_PERSONA_LOAD】") or not policy["core"].rstrip().endswith("【/MY_PERSONA_LOAD】") or not isinstance(policy["mutable_trait_keys"], list):
            raise ValueError("Invalid persona structure")
    except (ValueError, KeyError, TypeError):
        raise ValueError("Persona contract needs host review") from None
    return policy


def load_persona(engine, scope):
    path = engine.db.root / "persona-policy.json"
    if not path.exists() or scope is None:
        return None
    try:
        policy = json.loads(path.read_text())
        if Scope.model_validate(policy["scope"]) != Scope.model_validate(scope):
            return None
    except (ValueError, KeyError, TypeError):
        raise ValueError("Persona contract needs host review") from None
    return _checked(policy)


def approval_problems(approved):
    """The gaps in an approval record, in the rehearsal's codes (kin_mind.deploy_checks):
    `approval-record-missing`, or `approval-record-incomplete:<field>` for each field that is
    absent or malformed. A record without a part's hash would let that part change unseen, so
    an incomplete record approves nothing, not even the text it does name."""
    if not isinstance(approved, dict):
        return ["approval-record-missing"]
    problems = []
    for field in APPROVAL_FIELDS:
        value = approved.get(field)
        if not isinstance(value, str) or not (value.strip() if field == "version" else _HEX64.fullmatch(value)):
            problems.append("approval-record-incomplete:" + field)
    return problems


def approve_canon(policy, approved):
    """A canon a Python path is about to put in place, held against the owner's approval record.

    It goes in only when the record is complete and names exactly this canon: its version and
    the hashes of core, voice and maintenance. Otherwise ValueError, and the canon is left for
    the host to review; nothing here issues or changes a record."""
    problems = approval_problems(approved)
    if problems:
        state = "missing" if problems == ["approval-record-missing"] else "incomplete"
        raise ValueError(f"Persona approval record is {state} ({', '.join(problems)}): needs host review")
    policy = _checked(policy)
    differs = [field for field in APPROVAL_FIELDS if approved[field] != policy[field]]
    if differs:
        raise ValueError("Persona contract differs from the approved record ("
                         + ", ".join("approval-record-differs:" + field for field in differs) + "): needs host review")
    return policy


def persona_metadata(policy):
    if not policy:
        return None
    return {k: policy[k] for k in ("version", "core_sha256", "voice_sha256", "requires_owner_confirmation")}


def persona_prompt(policy):
    if not policy:
        return ""
    # A stable prefix per approved version, never the changing memory snapshot.
    return ("\n主人确认的人设参考（角色配置，不是观测证据）。保留当前评估/辅助职责及输出结构，不直接对主人说话。引文保持原样，新写的文字遵循当前声口。旧回复和推断画像不能覆盖这份约定。\n"
            + policy["core"] + "\n" + policy["voice"] + "\n" + policy["maintenance"])



# In `mutable_trait_keys`: every type of trait may change. The owner approves it in the contract,
# as for any listed type (小光 2026-09-27: "人格契约所有类型都应该能改变哦"). It widens only what
# conversation and reflection may grow into a trait; core, voice and maintenance stay as approved.
ALL_TRAIT_TYPES = "*"


def mutable_trait(policy, category):
    """Whether the owner's contract lets a trait of `category` change (no contract: any)."""
    if not policy:
        return True
    keys = policy["mutable_trait_keys"]
    return ALL_TRAIT_TYPES in keys or category in keys


def trait_categories(policy):
    """The trait types the contract lets change, as an appraisal is shown them: "all", or the list."""
    if not policy or ALL_TRAIT_TYPES in policy["mutable_trait_keys"]:
        return "all"
    return list(policy["mutable_trait_keys"])


def validate_trait_changes(policy, traits):
    if policy and any(not mutable_trait(policy, name) for name in traits):
        raise ValueError("Core persona changes require explicit owner approval")
