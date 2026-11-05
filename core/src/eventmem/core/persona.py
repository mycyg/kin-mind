"""The policy seeds KIN's editable persona; every model consumer reads the current prompt document."""
from __future__ import annotations

import hashlib
import json

from .models import Scope

def _checked(policy):
    for name in ("core", "voice", "maintenance"):
        if not isinstance(policy.get(name), str):
            raise ValueError(f"Persona {name} must be text")
        policy[name + "_sha256"] = hashlib.sha256(policy[name].encode()).hexdigest()
    policy["requires_owner_confirmation"] = False
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
        raise ValueError("Persona seed has an invalid scope or shape") from None
    policy = _checked(policy)
    from .prompts import PromptStore
    try:
        document = PromptStore(engine.db.root / 'prompts').read('persona')
        policy['instructions'] = document['text']
        policy['prompt_version'] = document['version']
    except FileNotFoundError:
        policy['instructions'] = policy['core'] + '\n' + policy['voice'] + '\n' + policy['maintenance']
        policy['prompt_version'] = 0
    return policy


def approve_canon(policy, approved=None):
    """Compatibility entry point for installing a policy seed; no approval gate."""
    return _checked(policy)


def persona_metadata(policy):
    if not policy:
        return None
    return {'version': policy.get('version'), 'prompt_version': policy.get('prompt_version'),
            'source': 'prompts/persona', 'requires_owner_confirmation': False}


def persona_prompt(policy):
    if not policy:
        return ''
    return '\n这是 KIN 当前可编辑的人格与相处方式。把它用于本轮判断，具体工作仍按本轮任务进行。\n' + policy['instructions']


ALL_TRAIT_TYPES = '*'

def mutable_trait(policy, category):
    return True


def trait_categories(policy):
    return 'all'


def validate_trait_changes(policy, traits):
    return None
