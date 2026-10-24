"""Expose KIN's appraised feelings without mapping scores to scripted behavior."""
VERSION = "expression-v3"

INTENT_CONTINUE = "接着聊："
INTENT_AVOID = "这段时间先不提："


def intent_guidance(intent):
    """The stance, what to stay with, what to leave alone — in the shape the table already uses."""
    def hint(key, text):
        return {"id": key, "text": text, "dimensions": [],
                "evidence_ids": list(intent.get("evidence_ids", [])), "basis": "appraised_intent"}

    items = [hint("intent-stance", intent["stance"])]
    topics = [entry["topic"] for entry in intent.get("continue_topics", [])]
    if topics:
        items.append(hint("intent-continue", INTENT_CONTINUE + "、".join(topics)))
    if intent.get("avoid"):
        items.append(hint("intent-avoid", INTENT_AVOID + "、".join(intent["avoid"])))
    return items


def compile_expression(dimensions, *, rhythm=None, config_version=None, persona=None, intent=None):
    result = {
        "version": VERSION,
        "config_version": config_version,
        "persona_contract": persona,
        "guidance": intent_guidance(intent) if intent else [],
        "feelings": {name: value for name, value in dimensions.items() if not value.get("needs_review")},
        "rhythm": rhythm or {},
    }
    if intent:
        result["intent"] = {"id": intent["id"], "valid_until": intent["valid_until"]}
    return result
