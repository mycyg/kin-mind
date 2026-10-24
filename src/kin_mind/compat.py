"""Only an explicit behavior-contract change makes a prior decision incompatible."""
from __future__ import annotations

BEHAVIOR_CONTRACT = "behavior-2"
BEHAVIOR_MODELS = "behavior_models"
CHAT_FIELDS = ("chat", "chat_effort")

def parts(mind, conn, state=None):
    return {'contract': BEHAVIOR_CONTRACT}


def stamp(mind, conn, state=None):
    return {'key': BEHAVIOR_CONTRACT, 'parts': parts(mind, conn, state)}


def holds(stored, current):
    """Was this written under a compatible behavior contract?
    Persona wording updates, dimension description tweaks, model switches, and OS/runtime
    bumps do not invalidate Kin's active wishes, plans, or decisions."""
    if not stored or not isinstance(stored, dict):
        return False
    old, new = stored.get("parts") or {}, current["parts"]
    if not isinstance(old, dict) or not old.get("contract"):
        return False
    return old["contract"] == new.get("contract")


def stale_reason(stored, current):
    """None while the stamp holds; otherwise a static code naming the ingredients that moved.

    Ingredient names only: nothing of the persona, the models or the environment reaches a row."""
    if holds(stored, current):
        return None
    old = (stored or {}).get("parts") or {}
    moved = [name for name in sorted(current["parts"]) if old.get(name) != current["parts"][name]]
    return "compat-changed:" + (",".join(moved) if old else "unstamped")

