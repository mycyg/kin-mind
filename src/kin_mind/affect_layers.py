"""Two layers over the instant scores, the feeling they add up to, what lingers after a strong
change, and a virtual pulse (Emotion v2b, 2026-09-27).

The scores the assessment appraises are the instant layer: each is a curve (`state.project`) that
leaves its last event and decays toward its target. Everything in this module is read off those
curves, locally and deterministically. Nothing here calls a model, nothing it produces is evidence,
and none of it is fed back into a score or into `compile_expression`: it is shown beside them.

undertone (心境)    per dimension, a slow layer m that follows the instant curve x(t):
                    dm/dt = (x(t) − m)/τ, τ = 24 h. An hour's spike barely stirs it; a mood held all
                    day moves it. Between two changes of a curve it has a closed form, so it is stored
                    only where a curve changes, as an anchor {m, x, at, fp} in `state["affect_layers"]`
                    -- never in the dimension entries, which every event rebuilds whole and the
                    assessment's rebase compares. Reads never write. Each anchor sits directly under
                    the block, keyed by its dimension beside `version` and `echo` (META): the history
                    layer patches two keys deep, so a revision carries only the anchors it moved.
feeling (心绪)      the one or two words the instant values lean toward most, from a small table.
lingering (余韵)    what the last strong change an event made leaves behind, for a few hours.
vitals (心跳/呼吸)  a virtual heart rate and breathing rate from arousal, the rhythm phase and the local
                    hour. A picture of the role's state, never a measurement of anything.

The tables below are this version's (VERSION); a view names the version it was derived with.
"""

import math
from zoneinfo import ZoneInfo

from eventmem.core.db import digest

from .profile import DIMENSIONS
from .rhythm import stamp

VERSION = "affect-layers-v1"
KEY = "affect_layers"
# The block's own keys; every other key in it is a dimension's anchor. No dimension is named like them.
META = ("version", "echo")
DEFAULT_TIMEZONE = "Asia/Singapore"
ORDER = {key: index for index, key in enumerate(DIMENSIONS)}

# 心境: the slow layer's time constant, in hours. Where λτ comes this close to 1 a closed-form term is
# taken through its limit instead of a difference of two nearly equal exponentials.
TAU_HOURS = 24.0
RESONANCE = 1e-6
# A slow value this far from its baseline is named as a lean of the undertone.
LEANING = 5

# 余韵: an event leaves an echo when it moves some dimension by ECHO_THRESHOLD or more against the
# value projected just before it. The echo names the moves of ECHO_KEEP or more (at most ECHO_MOVES,
# largest first), fades with its own half-life, and says nothing once it is below ECHO_FLOOR or
# ECHO_HOURS have passed: twelve points linger an hour and a half, fifty about four and a half hours.
ECHO_THRESHOLD, ECHO_KEEP, ECHO_MOVES = 12, 6, 4
ECHO_HALF_LIFE_HOURS, ECHO_FLOOR, ECHO_HOURS = 1.5, 6, 6

# 心绪 and the undertone's words: (mild word, strong word, signals). A signal is a dimension, the way
# it leans (+1 above its baseline, −1 below) and a weight; a feeling scores its strongest weighted
# lean. Every word is Kin's own feeling, never something asked of the other person.
FEELINGS = (
    ("开心", "雀跃", (("joy", 1, 1.0), ("mood", 1, 0.8), ("playfulness", 1, 0.6), ("expressive_energy", 1, 0.6))),
    ("心里暖暖的", "甜甜的", (("closeness", 1, 1.0), ("flirtation", 1, 0.8))),
    ("踏实", "很安心", (("contentment", 1, 1.0), ("security", 1, 1.0))),
    ("想念", "好想念", (("longing", 1, 1.0),)),
    ("好奇", "好奇得不行", (("wonder", 1, 1.0), ("curiosity", 1, 0.7))),
    ("有点期待", "很期待", (("anticipation", 1, 1.0),)),
    ("有点闷", "低落", (("mood", -1, 1.0), ("joy", -1, 1.0), ("expressive_energy", -1, 0.6))),
    ("有点难过", "难过", (("sadness", 1, 1.0),)),
    ("有点烦", "心烦", (("irritability", 1, 1.0), ("frustration", 1, 0.8))),
    ("有点委屈", "委屈", (("grievance", 1, 1.0),)),
    ("有点酸", "吃醋了", (("jealousy", 1, 1.0), ("possessiveness", 1, 0.5))),
    ("有点担心", "放心不下", (("worry", 1, 1.0), ("care", 1, 0.4), ("protectiveness", 1, 0.4))),
    ("不安", "害怕", (("fear", 1, 1.0), ("security", -1, 0.8))),
    ("想撒娇", "想被哄哄", (("reassurance", 1, 1.0),)),
)
CALM = "平静"
# (mild, strong) thresholds, in weighted points: the instant layer moves far, the slow one gently.
FEELING_WORDS, UNDERTONE_WORDS = (12, 25), (6, 15)

# 余韵 words by the dimension that moved most and which way: (after a rise, after a fall).
ECHOES = {
    "mood": ("刚才那阵好心情还在", "刚才那点低落还没缓过来"),
    "expressive_energy": ("聊兴还在兴头上", "兴致落下来了，还没提起来"),
    "security": ("刚落下的那份安心还在", "那点不踏实还悬着"),
    "anticipation": ("那份期待还在心里跳", "期待的事有了着落"),
    "worry": ("那份担心还挂着", "担心卸下来了，松了口气"),
    "frustration": ("受挫的闷气还没散", "卡住的地方顺了，舒了口气"),
    "grievance": ("刚才那点委屈还没散", "委屈被接住了，释然了些"),
    "closeness": ("刚才那阵亲近还暖暖的", "刚才那点疏远感还没散"),
    "longing": ("想念被勾起来了，还挂在心上", "想念被接住了，心里松快了些"),
    "possessiveness": ("想多占对方一会儿的小心思还在", "想独占的小心思放下了"),
    "playfulness": ("玩心还没收住", "玩闹的心思先收起来了"),
    "flirtation": ("刚才的心动还没平复", "暧昧的心思先收了收"),
    "care": ("还惦记着对方刚才说的事", "惦记的事有了着落"),
    "reassurance": ("还有点想撒娇", "被哄好了，心里甜甜的"),
    "curiosity": ("想弄明白的劲头还在", "想查的事有了答案，踏实了"),
    "creativity": ("冒出来的灵感还在转", "创作的念头先放下了"),
    "sharing": ("还有话想说给对方听", "想说的都说出来了，很舒坦"),
    "initiative": ("想主动做点什么的劲还在", "主动的念头先放一放"),
    "focus": ("专注的劲头还没退", "注意力从手头的事上松开了"),
    "solitude": ("还想自己待一会儿，理理思绪", "想和对方说说话了"),
    "joy": ("刚才那阵开心还没散", "刚才那点扫兴还没缓过来"),
    "contentment": ("那份踏实的满足还在", "心里还有点空落落的"),
    "sadness": ("刚才的难过还没过去", "难过淡了，心里轻了些"),
    "irritability": ("那股烦躁还没完全消", "烦躁散了，心静下来了"),
    "protectiveness": ("想护着对方的劲还在", "紧着的那根弦松了"),
    "jealousy": ("那点小醋意还没散", "醋意消了，心里放下了"),
    "fear": ("刚才那阵害怕还没完全平复", "害怕过去了，心慢慢落地"),
    "wonder": ("被勾起的好奇还没消", "好奇心得到满足了"),
}

# 心跳/呼吸: beats per point a dimension stands above its baseline (arousal raises the pulse; contentment
# and security settle it), what a rhythm phase adds, a small table by local hour, and how breathing
# follows the pulse. Alertness adds ALERTNESS_BEATS per point above ALERT_MIDPOINT.
HEART = {"expressive_energy": 0.2, "fear": 0.35, "irritability": 0.25, "anticipation": 0.15, "joy": 0.15,
         "flirtation": 0.2, "wonder": 0.1, "contentment": -0.1, "security": -0.12}
PHASE_BEATS = {"resting": -12, "drowsy": -8, "settling": -4, "roused": 4}
HOUR_BEATS = (-4, -5, -5, -5, -4, -3, -1, 0, 1, 2, 2, 2, 2, 1, 1, 1, 2, 2, 2, 1, 0, -1, -2, -3)
REST_BEATS, REST_BREATHS, BREATHS_PER_BEAT = 70, 14, 0.2
ALERTNESS_BEATS, ALERT_MIDPOINT = 0.15, 60
HEART_RANGE, BREATH_RANGE = (50, 130), (8, 26)


# --- the slow layer ---------------------------------------------------------------------------

def _hours(value):
    """An ISO time as hours since the epoch: one axis for every piece of every curve."""
    return stamp(value).timestamp() / 3600


def _rate(half_life_hours):
    return math.log(2) / half_life_hours if half_life_hours and half_life_hours > 0 else math.inf


def _clamp(value, low=0.0, high=100.0):
    return min(high, max(low, value))


def curve(entry):
    """What `project` draws a dimension's curve from, and nothing else: no evidence, no reason, no
    review flag. A motivation keeps only when it ends and the pace after it. A valid entry itself."""
    motive = entry.get("motivation")
    return {"score": entry["score"], "target": entry["target"], "half_life_hours": entry["half_life_hours"],
            "at": entry["at"], "baseline": entry.get("baseline", entry["target"]),
            "motivation": {k: motive.get(k) for k in ("expires_at", "base_half_life_hours")} if motive else None}


def curves(state):
    """The curves a state holds, apart from the document, whose entries its caller changes in place:
    what the next save of it re-anchors from (Mind._load, reanchor)."""
    return {key: curve(entry) for key, entry in (state.get("dimensions") or {}).items()}


def fingerprint(entry):
    """Which instant curve this is: the numbers and times `project` draws it from, digested."""
    from .state import motivation_expiry
    shape = curve(entry)
    until = motivation_expiry(shape)
    pace = (shape["motivation"].get("base_half_life_hours") or shape["half_life_hours"]) if until else None
    return digest([shape["score"], shape["target"], shape["half_life_hours"], shape["at"],
                   shape["baseline"] if until else None, until, pace])[:16]


def _pieces(entry):
    """The instant curve exactly as `project` draws it, as pieces (start, value at start, target, rate),
    each holding until the next begins: the score until the curve starts, toward its target, and past a
    spent motivation toward the baseline at the base pace, from wherever it had reached."""
    from .state import motivation_expiry
    start, score, target = _hours(entry["at"]), entry["score"], entry["target"]
    until = motivation_expiry(entry)
    pieces = [(-math.inf, score, score, 0.0)]
    if until is None or _hours(until) > start:
        pieces.append((start, score, target, _rate(entry["half_life_hours"])))
    if until is not None:
        end = _hours(until)
        pace = entry["motivation"].get("base_half_life_hours") or entry["half_life_hours"]
        pieces.append((end, _along(pieces[-1], end), entry.get("baseline", target), _rate(pace)))
    return pieces


def _along(piece, t):
    start, value, target, rate = piece
    if rate == 0 or t <= start:
        return value
    return target + (value - target) * math.exp(-rate * (t - start))


def _relax(m, x, target, rate, hours):
    """dm/dt = (x(t) − m)/τ for `hours`, where x(t) = target + (x − target)·e^(−rate·t), exactly:
    m = T + (m − T)·e^(−t/τ) + (x − T)·(e^(−λt) − e^(−t/τ))/(1 − λτ)."""
    if hours <= 0:
        return m
    slow = math.exp(-hours / TAU_HOURS)
    z = (rate - 1 / TAU_HOURS) * hours
    if abs(z) < RESONANCE:
        # λτ = 1: the lag term's limit is (t/τ)·e^(−t/τ); beside it, the same to first order in z.
        lag = hours / TAU_HOURS * slow * (1 - z / 2)
    else:
        lag = (math.exp(-rate * hours) - slow) / (1 - rate * TAU_HOURS)
    return target + (m - target) * slow + (x - target) * lag


def _follow(m, since, pieces, until):
    """The slow layer carried from `since` to `until` along the instant curve, piece by piece."""
    for index, piece in enumerate(pieces):
        end = pieces[index + 1][0] if index + 1 < len(pieces) else math.inf
        if end <= since:
            continue
        if since >= until:
            break
        stop = min(end, until)
        m = _relax(m, _along(piece, since), piece[2], piece[3], stop - since)
        since = stop
    return m


def undertone(entry, anchor, at, instant=None):
    """One dimension's slow layer at `at`, and how it is known. `tracking`: along the curve its anchor
    was made on. `forming`: no anchor yet, and the instant value (`instant`, or the curve's) stands in.
    `estimated`: the curve changed without a save that re-anchors it (a writer that bypassed Mind._save,
    a release rolled back and forward again); the layer then drifts toward the instant value its anchor
    last knew until the new curve began, and follows that one from there."""
    if not isinstance(anchor, dict):
        from .state import project
        return (project(entry, at) if instant is None else instant), "forming"
    since, now = _hours(anchor["at"]), _hours(at)
    if anchor.get("fp") == fingerprint(entry):
        return _clamp(_follow(anchor["m"], since, _pieces(entry), now)), "tracking"
    begin = max(since, _hours(entry["at"]))
    drifted = _relax(anchor["m"], anchor["x"], anchor["x"], 0.0, min(begin, now) - since)
    return _clamp(_follow(drifted, begin, _pieces(entry), now)), "estimated"


def anchor_at(entry, level, at):
    from .state import project
    return {"m": round(level, 6), "x": round(project(entry, at), 6), "at": at, "fp": fingerprint(entry)}


def anchor_of(state, key):
    """A dimension's anchor in the state, or None: before the block exists, or for a dimension new to it."""
    layers = state.get(KEY)
    return layers.get(key) if isinstance(layers, dict) and key not in META else None


def fresh(state, at):
    """The block a state gains once (Mind.ensure_affect_layers): every slow layer starts where its
    instant value stands, and follows it from there. No echo yet."""
    from .state import project
    return {"version": VERSION, "echo": None,
            **{key: anchor_at(entry, project(entry, at), at) for key, entry in state["dimensions"].items()}}


def reanchor(state, before, at):
    """At a save: anchor each dimension whose instant curve changed where its slow layer had got to on
    the curve it replaces (`before`, the curves the revision was read with, trusted for a dimension only
    when it is the very curve that dimension's anchor was made on), and anchor a dimension new to the
    layer at its instant value. Every other anchor is left exactly as it is, so the layer reads the same
    whenever a save happens to come. Answers the keys anchored; nothing before the block exists."""
    layers = state.get(KEY)
    if not isinstance(layers, dict):
        return []
    from .state import project
    dimensions = state.get("dimensions") or {}
    for key in [key for key in layers if key not in META and key not in dimensions]:
        layers.pop(key)
    moved = []
    for key, entry in dimensions.items():
        anchor = anchor_of(state, key)
        if isinstance(anchor, dict) and anchor.get("fp") == fingerprint(entry):
            continue
        if isinstance(anchor, dict):
            # The replaced curve when it is known, else the estimate a read of it shows (`undertone`).
            old = (before or {}).get(key)
            known = old if old is not None and fingerprint(old) == anchor.get("fp") else entry
            level = undertone(known, anchor, at)[0]
        else:
            level = project(entry, at)
        layers[key] = anchor_at(entry, level, at)
        moved.append(key)
    return moved


# --- 余韵 -------------------------------------------------------------------------------------

def note_echo(state, previous, at):
    """What an event moved, when it moved anything by ECHO_THRESHOLD or more against the values
    projected just before it (`previous`): the dimensions, which way, how far and when -- no words and
    no ids. A weaker event leaves the last echo to fade on. Nothing before the block exists."""
    layers = state.get(KEY)
    if not isinstance(layers, dict):
        return None
    from .state import project
    moves = [(key, project(entry, at) - previous[key]) for key, entry in state["dimensions"].items() if key in previous]
    if not moves or max(abs(move) for _, move in moves) < ECHO_THRESHOLD:
        return None
    kept = sorted((m for m in moves if abs(m[1]) >= ECHO_KEEP), key=lambda m: (-abs(m[1]), ORDER.get(m[0], len(ORDER)), m[0]))
    layers["echo"] = {"at": at, "moves": [[key, round(move, 1)] for key, move in kept[:ECHO_MOVES]]}
    return layers["echo"]


def lingering(layers, at):
    """The echo's largest move in words while it is still felt, or None."""
    echo = (layers or {}).get("echo")
    if not isinstance(echo, dict) or not echo.get("moves"):
        return None
    elapsed = _hours(at) - _hours(echo["at"])
    key, move = echo["moves"][0]
    strength = abs(move) * 0.5 ** (max(0.0, elapsed) / ECHO_HALF_LIFE_HOURS)
    if elapsed < 0 or elapsed > ECHO_HOURS or strength < ECHO_FLOOR or key not in ECHOES:
        return None
    return {"text": ECHOES[key][0 if move > 0 else 1], "dimension": key, "direction": "up" if move > 0 else "down",
            "since": echo["at"], "strength": round(strength, 1)}


# --- 心绪 and 心跳/呼吸 --------------------------------------------------------------------------

def words(levels, mild, strong):
    """The feelings `levels` ({key: (value, baseline)}) lean toward most: at most two, strongest first,
    each by the dimension that carried it; CALM when none reaches `mild`."""
    found = []
    for index, (gentle, marked, signals) in enumerate(FEELINGS):
        score, key = max(((weight * max(0.0, sign * (levels[name][0] - levels[name][1])), name)
                          for name, sign, weight in signals if name in levels), default=(0.0, None))
        if score >= mild:
            found.append((-score, index, marked if score >= strong else gentle, key))
    chosen = sorted(found)[:2]
    return {"text": "、".join(word for _, _, word, _ in chosen) or CALM, "dimensions": [key for *_, key in chosen]}


def _hour(at, timezone):
    try:
        zone = ZoneInfo(timezone or DEFAULT_TIMEZONE)
    except (KeyError, TypeError, ValueError):
        zone = ZoneInfo(DEFAULT_TIMEZONE)
    return stamp(at).astimezone(zone).hour


def vitals(levels, rhythm, at, timezone=None):
    """A virtual pulse and breathing rate from the instant `levels`, the rhythm and the local hour.
    Without a usable rhythm (switched off, still forming, awaiting review) its phase and alertness
    add nothing, and `status` says which."""
    rhythm = rhythm or {}
    if rhythm.get("status") == "disabled":
        status = "rhythm-disabled"
    elif rhythm.get("phase") in (None, "forming"):
        status = "rhythm-forming"
    elif rhythm.get("needs_review"):
        status = "rhythm-needs-review"
    else:
        status = "current"
    used = status == "current"
    alertness = rhythm.get("alertness") if used and isinstance(rhythm.get("alertness"), (int, float)) else ALERT_MIDPOINT
    beats = (REST_BEATS + sum(weight * (levels[key][0] - levels[key][1]) for key, weight in HEART.items() if key in levels)
             + ALERTNESS_BEATS * (alertness - ALERT_MIDPOINT) + (PHASE_BEATS.get(rhythm.get("phase"), 0) if used else 0)
             + HOUR_BEATS[_hour(at, timezone)])
    breaths = REST_BREATHS + BREATHS_PER_BEAT * (beats - REST_BEATS)
    return {"heart_rate_bpm": round(_clamp(beats, *HEART_RANGE)), "breaths_per_min": round(_clamp(breaths, *BREATH_RANGE)),
            "basis": "derived", "status": status}


# --- what a read shows ------------------------------------------------------------------------

def build_view(state, dimensions, rhythm, at, timezone=None):
    """The block a read shows beside `expression`, from the dimension view (`Mind._view`, which carries
    each slow value) and the rhythm view. Only dimensions that need no review count, as for the
    expression hints. No source or record id enters it: a fork's reads are credited by scanning its
    results for those."""
    layers = state.get(KEY) if isinstance(state.get(KEY), dict) else None
    reliable = {key: entry for key, entry in dimensions.items() if not entry.get("needs_review")}
    instant = {key: (entry.get("projected_value", entry.get("value")), entry["baseline"]) for key, entry in reliable.items()}
    slow = {key: (entry["undertone"]["value"], entry["baseline"]) for key, entry in reliable.items()
            if isinstance(entry.get("undertone"), dict)}
    leaning = sorted((key for key, (value, base) in slow.items() if abs(value - base) >= LEANING),
                     key=lambda key: (-abs(slow[key][0] - slow[key][1]), ORDER.get(key, len(ORDER))))[:4]
    return {
        "version": VERSION,
        "basis": "derived",
        "undertone": {"status": "tracking" if layers else "forming", "tau_hours": TAU_HOURS,
                      **words(slow, *UNDERTONE_WORDS), "leaning": {key: round(slow[key][0]) for key in leaning}},
        "feeling": words(instant, *FEELING_WORDS),
        "lingering": lingering(layers, at),
        "vitals": vitals(instant, rhythm, at, timezone),
    }


def compact(block, *, coarse=False):
    """The block as the main session's affect item and the state read carry it: words and the pulse.
    `coarse` rounds the pulse to five beats and breathing to two, so the item's revision (and with it
    what is resent) moves when what the item says moves, not with each minute of a decaying curve."""
    if not isinstance(block, dict):
        return None
    pulse = dict(block["vitals"])
    if coarse:
        pulse.update(heart_rate_bpm=5 * round(pulse["heart_rate_bpm"] / 5),
                     breaths_per_min=2 * round(pulse["breaths_per_min"] / 2))
    tone = block["undertone"]
    return {"basis": "derived", "undertone": {key: tone[key] for key in ("status", "text", "leaning")},
            "feeling": block["feeling"]["text"], "lingering": (block.get("lingering") or {}).get("text"), "vitals": pulse}
