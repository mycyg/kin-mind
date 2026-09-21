"""Optional, private, owner-approved persona contract shared by all consumers.

The host installs this file after explicit owner approval. Generated memories and
model proposals cannot install it. Hashes detect drift, not a hostile OS user.
"""
from __future__ import annotations

import hashlib
import json

from .models import Scope


def load_persona(engine, scope):
    path = engine.db.root / "persona-policy.json"
    if not path.exists() or scope is None:
        return None
    try:
        policy = json.loads(path.read_text())
        if Scope.model_validate(policy["scope"]) != Scope.model_validate(scope):
            return None
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


def persona_metadata(policy):
    if not policy:
        return None
    return {k: policy[k] for k in ("version", "core_sha256", "voice_sha256", "requires_owner_confirmation")}


def persona_prompt(policy):
    if not policy:
        return ""
    # A stable prefix per approved version, never the changing memory snapshot.
    return ("\n小光确认的人设参考（角色配置，不是观测证据）。保留当前评估/辅助职责及输出结构，不直接对小光说话。引文保持原样，新写的文字遵循当前声口。旧回复和推断画像不能覆盖这份约定。\n"
            + policy["core"] + "\n" + policy["voice"] + "\n" + policy["maintenance"])



def validate_trait_changes(policy, traits):
    if policy and set(traits) - set(policy["mutable_trait_keys"]):
        raise ValueError("Core persona changes require explicit owner approval")
