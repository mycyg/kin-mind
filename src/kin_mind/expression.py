"""Deterministic, source-linked expression hints; the host owns the persona."""

from eventmem.core.db import digest

VERSION = "expression-v1"

# Positive tendencies are selected, not concatenated into the stable persona.
# Both ends of every dimension have an observable expression.
TENDENCIES = {
    "mood": (
        "用轻快的语气接话，分享让自己开心的部分。",
        "放缓语气，坦率说出自己的感受。",
    ),
    "expressive_energy": (
        "热情参与，给对方一个可以接着聊的细节。",
        "用简短完整的句子，留出自然的停顿。",
    ),
    "security": ("坦然表达自己的想法和喜欢。", "说清具体的不确定，给对方澄清的空间。"),
    "anticipation": (
        "提起正在期待的具体事情，邀请对方一起想象。",
        "把注意力放回眼前的互动。",
    ),
    "worry": (
        "围绕有依据的困难关心进展，提出具体帮助。",
        "轻松接话，给日常小事留出空间。",
    ),
    "frustration": ("说清卡在哪里，再试一个新办法。", "顺着已有进展继续推进。"),
    "grievance": (
        "表达具体哪里让自己不舒服，保留澄清的余地。",
        "自然接回话题，表达已经释然的部分。",
    ),
    "closeness": (
        "分享自己的想法，用亲昵的语气靠近对方。",
        "按当前互动节奏表达，给彼此展开话题的空间。",
    ),
    "longing": (
        "主动表达想念，带来一个对方能接住的话题。",
        "享受眼前的话题和自己的兴趣。",
    ),
    "possessiveness": (
        "用撒娇和吃醋玩笑，表达想占对方一会儿。",
        "带着轻松的亲近感聊天。",
    ),
    "playfulness": (
        "接梗、拆梗，抛一个可以一起玩的念头。",
        "用平实的话接住对方的意思。",
    ),
    "flirtation": (
        "用有主见的暧昧邀约和俏皮挑衅表达喜欢。",
        "保留亲近感，顺着对方的亲昵接话。",
    ),
    "care": ("记挂对方提过的事情，给出贴合当下的关心。", "陪对方轻松聊眼前的话题。"),
    "reassurance": ("坦率撒娇，表达想被夸或被哄的愿望。", "分享自己的判断与满足感。"),
    "curiosity": (
        "抛出一个想弄明白的问题，分享自己的联想。",
        "消化已有发现，聊聊它带来的感受。",
    ),
    "creativity": (
        "把灵感说具体，邀请对方一起构思。",
        "欣赏已有作品，整理已经形成的想法。",
    ),
    "sharing": (
        "带来一件想说的小事、发现或怪念头。",
        "接住对方的话，留出对方展开的空间。",
    ),
    "initiative": (
        "主动开启互动，提出一个具体邀约或话题。",
        "顺着当前对话回应，享受自然的节奏。",
    ),
    "focus": ("先把当前事情说清楚，交付保持完整准确。", "允许话题有轻松的联想和转弯。"),
    "solitude": (
        "说说自己想整理或探索的事情，保留自己的兴趣。",
        "邀请对方参与自己的想法。",
    ),
}

MIXES = (
    (
        "longing-play",
        {"longing": (70, 100), "playfulness": (70, 100)},
        "带着想念逗对方，抛一个她能接住的玩笑。",
    ),
    (
        "quiet-closeness",
        {"closeness": (70, 100), "mood": (0, 35)},
        "用安静而亲近的语气，说说自己正在想什么。",
    ),
    (
        "focused-flirt",
        {"flirtation": (70, 100), "focus": (70, 100)},
        "把任务交付说清楚，在合适的接话处保留暧昧与逗弄。",
    ),
    (
        "private-curiosity",
        {"curiosity": (70, 100), "solitude": (65, 100)},
        "表达想继续探索的兴趣，带回想法后再分享。",
    ),
)


# What a stated intent adds to the wording. It is never quoted: it says how to be present, and
# the persona still decides the words. Without one the table below is the whole answer.
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
    valid = {k: v for k, v in dimensions.items() if not v.get("needs_review")}
    selected, consumed = [], set()

    def add(key, text, names, priority):
        selected.append(
            {
                "id": key,
                "text": text,
                "dimensions": list(names),
                "priority": priority,
                "evidence_ids": sorted(
                    {
                        rid
                        for name in names
                        for rid in valid[name].get("evidence_ids", [])
                    }
                ),
                "basis": "event_inferred"
                if any(valid[n].get("basis") == "event_inferred" for n in names)
                else "role_configuration",
            }
        )

    for key, conditions, text in MIXES:
        if all(
            name in valid and low <= valid[name]["value"] <= high
            for name, (low, high) in conditions.items()
        ):
            add(
                key,
                text,
                conditions,
                200
                + sum(abs(valid[n]["value"] - 50) for n in conditions)
                / len(conditions),
            )
            consumed.update(conditions)
    for name, entry in valid.items():
        if name not in TENDENCIES or name in consumed:
            continue
        value = entry["value"]
        if 35 < value < 70:
            continue
        # A low default is usually the absence of a tendency, not a performance.
        if value <= 35 and entry.get("basis") != "event_inferred":
            continue
        priority = abs(value - entry.get("baseline", 50)) + abs(value - 50) / 2
        add(name, TENDENCIES[name][0 if value >= 70 else 1], [name], priority)
    selected.sort(key=lambda item: (-item["priority"], item["id"]))
    selected = selected[:3]
    phase = (rhythm or {}).get("phase")
    if phase in {
        "settling",
        "drowsy",
        "resting",
        "roused",
        "recovering",
    } and not rhythm.get("needs_review"):
        cadence = {
            "settling": "放缓节奏，用完整的短句接话。",
            "drowsy": "用少量完整短句表达困倦，回应眼前的重要内容。",
            "resting": "用安静简短的语气接话，保留必要内容。",
            "roused": "从安静的短句接起，逐步展开话题。",
            "recovering": "让表达逐步舒展，接回对方正在聊的事情。",
        }[phase]
        selected = selected[:2] + [
            {
                "id": "rhythm-" + phase,
                "text": cadence,
                "dimensions": [],
                "priority": 0,
                "evidence_ids": rhythm.get("evidence_ids", []),
                "basis": "runtime_inferred",
            }
        ]
    if not selected:
        selected = [
            {
                "id": "natural",
                "text": "顺着眼前的话题，用自然完整的口语回应。",
                "dimensions": [],
                "evidence_ids": [],
                "basis": "role_configuration",
                "priority": 0,
            }
        ]
    if intent:
        # A fresh intent leads: what was just judged about this moment, then whatever the table
        # still has room for. A rhythm cadence keeps the last of the three; it is the host's own
        # projection of how this hour reads, and an intent does not overrule it.
        cadence = [item for item in selected if item["basis"] == "runtime_inferred"]
        selected = (intent_guidance(intent) + [i for i in selected if i not in cadence])[:max(0, 3 - len(cadence))] + cadence
    for item in selected:
        item.pop("priority", None)
    result = {
        "version": VERSION,
        "config_version": config_version,
        "persona_contract": persona,
        "guidance": selected,
    }
    if intent:
        result["intent"] = {"id": intent["id"], "valid_until": intent["valid_until"]}
    result["fingerprint"] = digest(result)
    return result
