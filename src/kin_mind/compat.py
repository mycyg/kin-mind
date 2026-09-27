"""What a behavioral check was made under, and whether that still holds.

`agent_version` moves for every unrelated edit, so requiring it to be equal meant no check ever
survived long enough to be used. This key names what actually decides how Kin behaves: the approved
persona, a declared behavior contract, the text of the dimension definitions (never their baselines
and half-lives, which the evolution this chain feeds is what moves), the model that appraises and
the model that answers, and the part of the execution environment a behavior can depend on. An
unrelated version bump keeps a prediction testable; a real change makes it stale, visibly, and the
reason names the ingredient that moved.

`BEHAVIOR_CONTRACT` is declared, never derived: changing a behavior-relevant instruction is a
decision, not a side effect of editing prose. Whoever changes what Kin is asked to do bumps it
when the earlier checks and decisions no longer describe this agent.

Adding dimensions is not changing one. When emotion v2 added eight dimensions (2026-09-27 09:08
UTC) the definitions digest moved although no definition did, and every open prediction, every
confirmed check and every decision stamped before it went stale at once, silently: nothing had
changed about what any of them was made under. A release that adds dimensions declares them in
`DEFINITION_ADDITIONS`; a stamp whose definitions are exactly the current ones without the groups
added since still holds, when nothing else moved. Changing, removing or renaming a definition
still moves the stamp, and so does an addition that is not declared.

The stamp also fences decisions: a wish, a plan step or a method decided under one stamp holds
while it holds (state.Mind.decision_current), so a deployment that only moves agent_version no
longer voids them (K1-13, MAIN-RUA-02).
"""

from __future__ import annotations

import json

from eventmem.core.db import digest
from eventmem.core.persona import load_persona

# Bumped by hand. Every open prediction and pending proposal made under the previous contract
# becomes stale; none is deleted.
BEHAVIOR_CONTRACT = "behavior-2"
# The approved persona: its version and the hashes of the three texts the contract already carries.
PERSONA_FIELDS = ("version", "core_sha256", "voice_sha256", "maintenance_sha256")
# What a dimension entry holds that an evolution may move. A definition change asks a different
# question; a baseline change is the answer this chain exists to produce.
EVOLVING = ("baseline", "half_life_hours")
# The models a behavioral check rests on. The one that appraises is pinned by the evaluator itself
# and read from there. The one that answers is known only to the host, which registers it here.
BEHAVIOR_MODELS = "behavior_models"
CHAT_FIELDS = ("chat", "chat_effort")
# What an ingredient nothing has written yet digests to. It is a literal rather than an empty value
# so that a store which never registered one is stable: the first registration moves the key once.
UNREGISTERED = "unregistered"
# The execution facts a behavior can depend on. The rest of the environment is noise for this key.
ENVIRONMENT_KEYS = ("python", "node", "os", "platform", "shell")
# The dimensions releases added to the role profile, one group per release, oldest first. Declared,
# like BEHAVIOR_CONTRACT: a release that adds dimensions adds its group here, or bumps the contract
# when the addition does change what the earlier checks meant.
DEFINITION_ADDITIONS = (
    # Emotion system v2 (profile-dimensions-added, 2026-09-27).
    ("joy", "contentment", "sadness", "irritability", "protectiveness", "jealousy", "fear", "wonder"),
)


def _definitions(dimensions, without=frozenset()):
    """The digest of what the dimension definitions ask, less the dimensions in `without`."""
    return digest({key: {name: value for name, value in entry.items() if name not in EVOLVING}
                   for key, entry in dimensions.items() if key not in without})


def extended(dimensions):
    """The definitions digests the current ones extend only by declared additions: for each release
    that added a group, the current definitions without that group and every later one. Empty when
    the profile holds none of them."""
    found = []
    for index in range(len(DEFINITION_ADDITIONS)):
        added = frozenset(key for group in DEFINITION_ADDITIONS[index:] for key in group)
        if added & set(dimensions):
            found.append(_definitions(dimensions, added))
    return found


def _setting(conn, key):
    row = conn.execute("SELECT data FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else {}


def parts(mind, conn, state=None):
    """The ingredients, each already a digest, so a stored stamp names what moved and holds no text."""
    from . import appraisal
    profile = (state or mind._load(conn))["profile"]
    policy = load_persona(mind.engine, mind.scope)
    chat, environment = _setting(conn, BEHAVIOR_MODELS), _setting(conn, "execution_environment")
    running = {key: environment[key] for key in ENVIRONMENT_KEYS if key in environment}
    return {
        "persona": digest([policy[field] for field in PERSONA_FIELDS]) if policy else "none",
        "contract": BEHAVIOR_CONTRACT,
        "definitions": _definitions(profile["dimensions"]),
        "models": digest([appraisal.APPRAISAL_MODEL, appraisal.APPRAISAL_EFFORT,
                          *[chat.get(field) or UNREGISTERED for field in CHAT_FIELDS]]),
        "environment": digest(running or UNREGISTERED),
    }


def stamp(mind, conn, state=None):
    """What a claim, a prediction, an assessment or a pending proposal is written with. `extends`,
    when there is one, lists the definitions digests the current ones only add dimensions to; it is
    not part of the key."""
    state = state or mind._load(conn)
    ingredients = parts(mind, conn, state)
    found = {"key": "compat_" + digest(ingredients)[:32], "parts": ingredients}
    earlier = extended(state["profile"]["dimensions"])
    if earlier:
        found["extends"] = earlier
    return found


def holds(stored, current):
    """Was this written under the configuration that is running now? The same key, or the same
    ingredients but for definitions the current ones only add declared dimensions to."""
    if not stored:
        return False
    if stored.get("key") == current["key"]:
        return True
    old, new = stored.get("parts") or {}, current["parts"]
    return (bool(old) and set(old) == set(new)
            and all(old[name] == new[name] for name in new if name != "definitions")
            and old.get("definitions") in (current.get("extends") or ()))


def stale_reason(stored, current):
    """None while the stamp holds; otherwise a static code naming the ingredients that moved.

    Ingredient names only: nothing of the persona, the models or the environment reaches a row."""
    if holds(stored, current):
        return None
    old = (stored or {}).get("parts") or {}
    moved = [name for name in sorted(current["parts"]) if old.get(name) != current["parts"][name]]
    return "compat-changed:" + (",".join(moved) if old else "unstamped")

