"""The interaction projection of the state (`read` with `projection: "interaction"`).

A proactive contact draft reads the state for two things only: what the host renders for Kin when
the memory context is off -- `interactionView` in adapters/owner-host.mjs, which also serves an
ordinary turn -- and, with the memory context on, `contact` and `interaction_timing` alone. The
whole `read` answer carried every wish that ever finished, every concern with its evidence, the
exploration findings and the appraisal queue besides; on 2026-09-28 it had grown past the resident
worker's frame guard and every draft failed before it started (`mind-worker-output-limit`).

This is that part of the state and nothing else: what `interactionView` renders, cut to the same
bounds (INTERACTION_LIMITS here mirror its own), so that `interactionView` over this projection is
`interactionView` over the full read, key for key. Besides what is rendered it keeps only what the
rendered words were written from, where the render leaves it out: a wish's evidence, the
`<field>_evidence_ids` of a copied reason, and -- only while a dimension's reason is rendered, that
is while no expression is shown -- the evidence of that reason. The host names the ids of this
projection as what the draft was shown (`shown_ids`, CL6D-MM-01), so an id is here exactly when Kin
was shown it or words resting on it: a finished wish, an unselected concern or a dimension's
evidence behind an expression were never shown and are not named any more.
"""

from copy import deepcopy

PROJECTION = "interaction"
# The bounds `interactionView` renders (adapters/owner-host.mjs INTERACTION_LIMITS), and the
# number of exploration decisions it shows.
INTERACTION_LIMITS = {"desires": 16, "concerns": 6, "guidance": 3, "exploration_decisions": 4}
# The wishes it shows: those still wanted, waiting or under way, and not expired.
ACTIVE_DESIRE_STATUSES = ("wanted", "waiting", "in_progress")
# What it renders of a wish, and what those words were written from (its evidence, and inside
# `contact_wait` a copied reason's `reason_evidence_ids`, which the render strips).
DESIRE_FIELDS = ("id", "kind", "status", "topic", "content", "completion", "expires_at", "concern_ids",
                 "concern_needs_review", "contact_wait", "exploration_target", "exploration_id",
                 "needs_review", "trait_needs_review", "evidence")
# Whole, as the render shows them.
WHOLE_FIELDS = ("scope", "as_of", "revision", "agent_version", "profile_version", "persona_contract",
                "contact", "interaction_timing", "continuity", "traits", "interaction_style", "contact_unconfirmed")
RHYTHM_FIELDS = ("mode", "status", "phase", "alertness", "needs_review", "observed_at")


def expression_shown(view):
    """Whether the render shows an expression -- and with it no dimension's reason -- as
    `interactionView` decides it: an expression, and continuity not in shadow."""
    return (view.get("continuity") or {}).get("activation") != "shadow" and bool(view.get("expression"))


def _pick(value, keys):
    """The keys of `value` among `keys` that it has: one it lacks stays absent, as the render leaves it."""
    return {key: deepcopy(value[key]) for key in keys if key in value}


def _dimension(entry, reason_shown):
    value = _pick(entry, ("value", "basis", "needs_review", "reason"))
    if entry.get("undertone"):
        value["undertone"] = _pick(entry["undertone"], ("value",))
    if reason_shown:
        # The reason is rendered: what it was written from goes with it, under the store's name for
        # a copied field's sources, and is never rendered itself.
        value["reason_evidence_ids"] = list(entry.get("evidence_ids") or [])
    return value


def interaction_projection(view):
    """The bounded part of a state view (`Mind.read`) that `interactionView` reads."""
    shown = expression_shown(view)
    projected = {"projection": PROJECTION}
    projected.update(_pick(view, WHOLE_FIELDS))
    projected["dimensions"] = {key: _dimension(entry, not shown) for key, entry in (view.get("dimensions") or {}).items()}
    active = [d for d in view.get("desires") or []
              if not d.get("expired") and d.get("status") in ACTIVE_DESIRE_STATUSES]
    projected["desires"] = [_pick(d, DESIRE_FIELDS) for d in active[-INTERACTION_LIMITS["desires"]:]]
    expression = deepcopy(view.get("expression"))
    if isinstance(expression, dict) and isinstance(expression.get("guidance"), list):
        expression["guidance"] = expression["guidance"][:INTERACTION_LIMITS["guidance"]]
    projected["expression"] = expression
    projected["exploration_decisions"] = deepcopy((view.get("exploration_decisions") or [])[:INTERACTION_LIMITS["exploration_decisions"]])
    projected["selected_concerns"] = deepcopy((view.get("selected_concerns") or [])[:INTERACTION_LIMITS["concerns"]])
    understanding = (view.get("appraisal_summary") or {}).get("understanding")
    projected["appraisal_summary"] = {"understanding": deepcopy(understanding)} if understanding else None
    rhythm = view.get("rhythm")
    projected["rhythm"] = _pick(rhythm, RHYTHM_FIELDS) if isinstance(rhythm, dict) else rhythm
    layers = view.get("affect_layers")
    if isinstance(layers, dict):
        # The words and leanings of the derived layers and the virtual pulse; they name no source.
        projected["affect_layers"] = {
            **({"undertone": _pick(layers["undertone"], ("status", "text", "leaning"))} if isinstance(layers.get("undertone"), dict) else {}),
            **{name: _pick(layers[name], ("text",)) for name in ("feeling", "lingering") if isinstance(layers.get(name), dict)},
            **_pick(layers, ("vitals",)),
        }
    else:
        projected["affect_layers"] = layers
    return projected
