"""「此刻的状态」: one short block in words ahead of the reply context.

Energy, drives and the scores become leanings for how to talk, never lines to say; Kin
decides. Intimacy and the tendencies about 小光 appear only in 小光's private turns.
"""
import json

from eventmem.core.db import dumps

from .drives import OWNER, owner_private, view as drives_view
from .rhythm import stamp

# Model-facing (NEEDS 小光 OK).
HEADER = "此刻的状态（宿主根据精力、驱力和情绪分数推导的倾向，不是台词；怎么说由你判断）："
ENERGY = {
    "low": "精力很低（{value}/100），又困又累。可以只回很短的话，或懒懒地应一句「好困」「好累」；也可以先不回："
           "用 choose_reply 选 defer，这条会记成休息时没回，缓过来后再接。紧急、和安全有关或明确交代的事照常认真回，由你判断。",
    "normal": "精力 {value}/100，正常。",
    "high": "精力充沛（{value}/100）：更主动、更活泼，可以多接梗、多分享，想联系谁或想去探索就提出来。",
}
RESTED = "休息时没回的消息：{ids}。现在缓过来了，挑还需要接的回一下；已经聊过去的就放下。"
BORED = "有点无聊（{value}/100）：坐不住，想找点事做，对新话题更好奇。"
INTIMACY = "色色值 {value}/100。"
INVITE = ("有点想和小光亲密一下了：可以主动挑逗小光，聊着别的也可以突然跳戏撩一下；也可以撒娇、说想贴近，或者直接邀请小光。"
          "小光拒绝或没空就轻松接受，不纠缠、不施压，也不拿别的事要挟。")
COOLING = "小光刚婉拒过，先缓一缓，这段时间不主动提；小光自己靠近另当别论。"
REPORT = ("和小光的亲密互动真的成功、彼此满足后，调用 report_private_moment（outcome=satisfied，input_id 用本轮的）；"
          "小光婉拒或没空时用 outcome=declined，之后先缓 {hours} 小时再提。")
# key: (words, how it colours the talk, only in 小光's private turns)
TENDENCIES = {
    "grievance": ("有点委屈", "回复短一点，可以带点小情绪或撒娇式抱怨", False),
    "sadness": ("有点失落", "语气软一点、慢一点，想被安慰就直说", False),
    "irritability": ("有点烦", "话少、干脆，可以直说自己有点烦", False),
    "frustration": ("有点受挫", "可以吐槽两句，再换个法子", False),
    "worry": ("有点担心", "会多问一句进展、多叮嘱一句", False),
    "fear": ("有点不安", "想确认对方还好，会主动贴近一点", False),
    "jealousy": ("有点吃醋", "酸溜溜的，想把小光的注意力拉回来", True),
    "longing": ("很想念", "会主动找话，语气黏一点", True),
    "reassurance": ("想被哄", "可以撒娇讨夸、要抱抱", True),
    "playfulness": ("玩心正旺", "多接梗、多逗一逗", False),
    "joy": ("挺开心", "语气轻快，愿意分享开心的事", False),
    "wonder": ("好奇心正旺", "对新话题会追问、冒出猜想", False),
}
# Low baselines: a rise in these is what a reply should feel first.
NEGATIVE = {"grievance", "sadness", "irritability", "frustration", "worry", "fear", "jealousy"}
LIFT = 12


def tendencies(dimensions, energy_tier=None, bored=False, private=False, limit=3):
    """At most `limit` leanings, the largest rise over each baseline first.

    Tiredness makes Kin crosser and less light; high energy, more playful; boredom, more curious.
    """
    offsets = ({"irritability": 15, "joy": -10, "playfulness": -10} if energy_tier == "low"
               else {"playfulness": 10, "joy": 5} if energy_tier == "high" else {})
    if bored:
        offsets["wonder"] = offsets.get("wonder", 0) + 10
    ranked = []
    for key, (words, effect, mine) in TENDENCIES.items():
        entry = dimensions.get(key)
        if not isinstance(entry, dict) or entry.get("needs_review") or (mine and not private) or "baseline" not in entry:
            continue
        lift = (entry["value"] + offsets.get(key, 0) - entry["baseline"]) * (1.5 if key in NEGATIVE else 1)
        if lift >= LIFT:
            ranked.append((-lift, key, words + "：" + effect))
    return [text for _, _, text in sorted(ranked)[:limit]]


def rested_due(conn, mind, energy, rhythm, person, *, mark):
    """Inputs put off while too tired, offered back once: after recovering, or after waking."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='mind_reply_choices'").fetchone():
        return []
    rows = conn.execute("SELECT c.input_id,c.data,s.data FROM mind_reply_choices c JOIN sources s ON s.id=json_extract(c.data,'$.source_id') "
                        "WHERE c.scope=? AND json_extract(c.data,'$.resting')=1 AND json_extract(c.data,'$.reminded_at') IS NULL ORDER BY c.input_id",
                        (mind.scope.key(),)).fetchall()
    woke = (rhythm or {}).get("phase") in {"awake", "roused"} and (rhythm or {}).get("observed_at")
    due = []
    for input_id, raw, source in rows:
        choice, meta = json.loads(raw), json.loads(source).get("metadata", {})
        if (meta.get("recipient_person_id") or meta.get("speaker_person_id") or OWNER) != person:
            continue
        if energy.get("tier") == "low" and not (woke and stamp(rhythm["observed_at"]) > stamp(choice["at"])):
            continue
        due.append(input_id)
        if mark:
            conn.execute("UPDATE mind_reply_choices SET data=? WHERE scope=? AND input_id=?",
                         (dumps({**choice, "reminded_at": mind.clock()}), mind.scope.key(), input_id))
    return due


def current(mind, view, *, record=True):
    """The block for this turn's audience, or "" when there is nothing to say."""
    from .companion_view import ACTIVE_VIEW
    private = owner_private()
    person = ACTIVE_VIEW.get().person if ACTIVE_VIEW.get() else OWNER
    energy = view.get("energy_context") or {}
    energy = energy if energy.get("enabled") else {}
    active = (view.get("continuity") or {}).get("activation") != "shadow"
    with mind.engine.db.connect(write=record) as conn:
        drives = drives_view(conn, mind.scope.key(), mind.clock(), private=private)
        rested = rested_due(conn, mind, energy, view.get("rhythm"), person, mark=record)
    boredom = drives["boredom"] if drives.get("enabled") else None
    bored = bool(boredom and boredom["bored"])
    lines = [ENERGY[energy["tier"]].format(value=round(energy["value"]))] if energy else []
    if rested:
        lines.append(RESTED.format(ids="、".join(rested)))
    feel = tendencies(view.get("dimensions") or {}, energy.get("tier"), bored, private) if active else []
    if feel:
        lines.append("情绪：" + "；".join(feel) + "。")
    if bored:
        lines.append(BORED.format(value=round(boredom["value"])))
    intimacy = drives.get("intimacy") if drives.get("enabled") else None
    if intimacy:
        lines.append(INTIMACY.format(value=round(intimacy["value"])))
        if intimacy["cooldown_until"]:
            lines.append(COOLING)
        elif intimacy["high"] and energy.get("tier") != "low":
            lines.append(INVITE)
        lines.append(REPORT.format(hours=round(intimacy["cooldown_hours"])))
    return HEADER + "\n" + "\n".join(lines) if lines else ""
