"""What a behavioral check was made under, and whether that still holds.

`agent_version` moves for every unrelated edit, so requiring it to be equal meant no check ever
survived long enough to be used. This key names what actually decides how Kin behaves: the approved
persona, a declared behavior contract, the text of the dimension definitions (never their baselines
and half-lives, which the evolution this chain feeds is what moves), the models that appraise and
that answer, and the part of the execution environment a behavior can depend on. An unrelated
version bump keeps a prediction testable; a real change makes it stale, visibly, and the reason
names the ingredient that moved.

`BEHAVIOR_CONTRACT` is declared, never derived: changing a behavior-relevant instruction is a
decision, not a side effect of editing prose. `tests/test_behavior_chain.py` pins the digest of
those paragraphs against the contract, so an edit fails until its author chooses — bump the
contract, because the earlier checks no longer describe this agent, or re-pin, because they do.
"""

from __future__ import annotations

import json

from eventmem.core.db import digest
from eventmem.core.persona import load_persona

# Bumped by hand. Every open prediction and pending proposal made under the previous contract
# becomes stale; none is deleted.
BEHAVIOR_CONTRACT = "behavior-1"
# The approved persona: its version and the hashes of the three texts the contract already carries.
PERSONA_FIELDS = ("version", "core_sha256", "voice_sha256", "maintenance_sha256")
# What a dimension entry holds that an evolution may move. A definition change asks a different
# question; a baseline change is the answer this chain exists to produce.
EVOLVING = ("baseline", "half_life_hours")
# The configured roles a behavioral check rests on: the model that appraises, the model that answers.
MODEL_ROLES = ("summary", "answer")
# The execution facts a behavior can depend on. The rest of the environment is noise for this key.
ENVIRONMENT_KEYS = ("python", "node", "os", "platform", "shell")


def _setting(conn, key):
    row = conn.execute("SELECT data FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else {}


def parts(mind, conn, state=None):
    """The ingredients, each already a digest, so a stored stamp names what moved and holds no text."""
    profile = (state or mind._load(conn))["profile"]
    policy = load_persona(mind.engine, mind.scope)
    models, environment = _setting(conn, "models"), _setting(conn, "execution_environment")
    return {
        "persona": digest([policy[field] for field in PERSONA_FIELDS]) if policy else "none",
        "contract": BEHAVIOR_CONTRACT,
        "definitions": digest({key: {name: value for name, value in entry.items() if name not in EVOLVING}
                               for key, entry in profile["dimensions"].items()}),
        "models": digest([(models.get(role) or {}).get("model") for role in MODEL_ROLES]),
        "environment": digest({key: environment[key] for key in ENVIRONMENT_KEYS if key in environment}),
    }


def stamp(mind, conn, state=None):
    """What a claim, a prediction, an assessment or a pending proposal is written with."""
    ingredients = parts(mind, conn, state)
    return {"key": "compat_" + digest(ingredients)[:32], "parts": ingredients}


def holds(stored, current):
    """Was this written under the configuration that is running now?"""
    return bool(stored) and stored.get("key") == current["key"]


def stale_reason(stored, current):
    """None while the stamp holds; otherwise a static code naming the ingredients that moved.

    Ingredient names only: nothing of the persona, the models or the environment reaches a row."""
    if holds(stored, current):
        return None
    old = (stored or {}).get("parts") or {}
    moved = [name for name in sorted(current["parts"]) if old.get(name) != current["parts"][name]]
    return "compat-changed:" + (",".join(moved) if old else "unstamped")


def behavior_prompts():
    """The instructions a behavioral check is made under: the shared appraisal prompt and the
    paragraph of every audited section. Imported lazily; this module is on the commit path."""
    from . import appraisal
    return (appraisal.SYSTEM, *(appraisal.SECTION_PROMPTS[name] for name in appraisal.AUDIT_SECTIONS))


def prompt_digest():
    return digest(list(behavior_prompts()))
