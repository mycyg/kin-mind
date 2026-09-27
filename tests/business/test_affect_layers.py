"""Emotion system v2b (2026-09-27): what is derived from the instant scores -- the slow undertone
(心境), the feeling in words (心绪), what lingers after a strong change (余韵) and a virtual pulse
(心跳/呼吸).

All of it is computed locally from the committed state. Nothing here calls a model, nothing it
produces is evidence, and none of it is fed back into a score or into the expression hints.
"""
import inspect
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from eventmem.core.db import digest
from kin_mind import affect_layers as L
from kin_mind.continuity import ContinuityConfig, RhythmProposal
from kin_mind.profile import DIMENSIONS
from kin_mind.state import AffectiveEvent, Motivation, project

from test_kin_mind import event, setup, wish  # noqa: F401  (the synthetic mind fixture)

T0 = datetime(2026, 9, 27, tzinfo=timezone.utc)


def at(hours):
    return (T0 + timedelta(hours=hours)).isoformat()


def raw(mind):
    with mind.engine.db.connect() as conn:
        return mind._load(conn)


def live(mind):
    with mind.engine.db.connect() as conn:
        return conn.execute("SELECT data FROM mind_state WHERE scope=?", (mind.scope.key(),)).fetchone()[0]


def anchors(state):
    """The anchors a raw state holds, by dimension: every key of the block but its own."""
    return {key: value for key, value in state[L.KEY].items() if key not in L.META}


def migrated(mind):
    assert mind.ensure_affect_layers(agent_version="synthetic-v1")["state"] == "added"
    return raw(mind)


def policy(mind, source):
    """The action policy production runs with: initiative is no longer retargeted by every write."""
    from kin_mind.actions import ActionEvents
    ActionEvents(mind).configure({"command_id": "policy", "agent_version": "synthetic-v1", "expected_revision": mind.read()["revision"],
                                  "evidence_ids": [source("policy")], "reason": "The owner allowed autonomous review"})


def later(clock, **delta):
    clock[0] += timedelta(**delta)


def integrate(entry, m, start, end, step=0.01):
    """dm/dt = (x − m)/τ by fourth-order Runge–Kutta on `project` itself, over a grid that lands on
    every breakpoint the cases below place: the reference the closed form has to meet."""
    x = lambda hours: project(entry, at(hours))
    steps = round((end - start) / step)
    for index in range(steps):
        h = start + index * step
        k1 = (x(h) - m) / L.TAU_HOURS
        k2 = (x(h + step / 2) - (m + step / 2 * k1)) / L.TAU_HOURS
        k3 = (x(h + step / 2) - (m + step / 2 * k2)) / L.TAU_HOURS
        k4 = (x(h + step) - (m + step * k3)) / L.TAU_HOURS
        m += step / 6 * (k1 + 2 * k2 + 2 * k3 + k4)
    return m


RESONANT = L.TAU_HOURS * 0.6931471805599453  # λτ = 1
CURVES = {
    "a spike decaying to its baseline": {"score": 95, "target": 60, "half_life_hours": 2, "at": at(0), "baseline": 60},
    "a slow rise": {"score": 20, "target": 75, "half_life_hours": 48, "at": at(0), "baseline": 75},
    "the resonant pace": {"score": 90, "target": 30, "half_life_hours": RESONANT, "at": at(0), "baseline": 30},
    # Close enough that short spans take the limit and long ones the direct form: both meet the integral.
    "just beside it": {"score": 90, "target": 30, "half_life_hours": RESONANT * (1 + 1e-5), "at": at(0), "baseline": 30},
    "a curve that starts later, with a drive that runs out": {
        "score": 60, "target": 5, "half_life_hours": 0.5, "at": at(1), "baseline": 35,
        "motivation": {"target": 5, "half_life_minutes": 30, "expires_at": at(3), "base_half_life_hours": 2}},
    "a drive already spent when its curve began": {
        "score": 50, "target": 90, "half_life_hours": 1, "at": at(2), "baseline": 30,
        "motivation": {"target": 90, "half_life_minutes": 60, "expires_at": at(1), "base_half_life_hours": 4}},
}


@pytest.mark.parametrize("name", CURVES)
def test_the_closed_form_is_the_integral_of_the_instant_curve(name):
    entry = CURVES[name]
    for hours in (0, 0.4, 1, 1.7, 2.5, 3, 5, 12, 30):
        piece = [p for p in L._pieces(entry) if p[0] <= L._hours(at(hours))][-1]
        assert L._along(piece, L._hours(at(hours))) == pytest.approx(project(entry, at(hours)), abs=1e-9)
    anchor = {"m": 42.0, "x": project(entry, at(0)), "at": at(0), "fp": L.fingerprint(entry)}
    m, reached = 42.0, 0.0
    for hours in (0.5, 3, 10, 30):
        m, reached = integrate(entry, m, reached, hours), hours
        closed, known = L.undertone(entry, anchor, at(hours))
        assert known == "tracking" and closed == pytest.approx(m, abs=1e-7), hours


def test_reanchoring_anywhere_gives_the_same_result():
    entry = CURVES["a curve that starts later, with a drive that runs out"]
    anchor = L.anchor_at(entry, 70.0, at(0))
    straight = L.undertone(entry, anchor, at(20))[0]
    for hours in (0.25, 1, 2.9, 3, 3.1, 8, 19.5):
        again = L.anchor_at(entry, L.undertone(entry, anchor, at(hours))[0], at(hours))
        assert L.undertone(entry, again, at(20))[0] == pytest.approx(straight, abs=1e-6)
        # A save that moved nothing keeps the anchor it had, exactly.
        state = {"dimensions": {"joy": entry}, L.KEY: {"version": L.VERSION, "echo": None, "joy": deepcopy(again)}}
        assert L.reanchor(state, L.curves(state), at(hours + 1)) == [] and state[L.KEY]["joy"] == again


def test_a_save_trusts_only_the_curve_its_anchor_was_made_on():
    """The curves a revision was read with are the old ones only for a dimension whose anchor follows
    that very curve. Any other (a stale read) is not trusted: the save anchors what a read shows."""
    made = CURVES["a spike decaying to its baseline"]
    anchor = L.anchor_at(made, 50.0, at(0))
    stale, now = {**made, "score": 20, "at": at(1)}, {**made, "score": 80, "at": at(3)}

    def saved(before):
        state = {"dimensions": {"joy": now}, L.KEY: {"version": L.VERSION, "echo": None, "joy": deepcopy(anchor)}}
        assert L.reanchor(state, before, at(5)) == ["joy"]
        return state[L.KEY]["joy"]

    shown, known = L.undertone(now, anchor, at(5))
    assert known == "estimated" and saved({"joy": L.curve(stale)})["m"] == pytest.approx(shown, abs=1e-6)
    assert saved({})["m"] == pytest.approx(shown, abs=1e-6)
    followed = saved({"joy": L.curve(made)})
    assert followed["m"] == pytest.approx(L.undertone(made, anchor, at(5))[0], abs=1e-6)
    assert followed["fp"] == L.fingerprint(now) and followed["x"] == pytest.approx(project(now, at(5)), abs=1e-6)


def test_a_spike_and_a_lasting_mood_are_distinct(setup):
    mind, source, clock = setup
    migrated(mind)
    mind.record(event(mind, source, "one-good-moment", {"joy": 95}))
    peak = 0
    for hour in range(1, 25):
        later(clock, hours=1)
        peak = max(peak, mind.read()["dimensions"]["joy"]["undertone"]["value"])
    joy = mind.read()["dimensions"]["joy"]
    assert peak < 64 and joy["value"] == 60 and joy["undertone"]["value"] < 62, "an hour's spike barely stirs the undertone"
    for index in range(12):
        mind.record(event(mind, source, "a-good-day-" + str(index), {"joy": 90}))
        later(clock, hours=2)
    view = mind.read()
    assert view["dimensions"]["joy"]["undertone"]["value"] > 70, "a day of it moves the undertone"
    assert view["affect_layers"]["undertone"]["leaning"]["joy"] > 70
    assert view["affect_layers"]["undertone"]["text"].split("、")[0] in {"开心", "雀跃"}
    later(clock, hours=4)
    joy = mind.read()["dimensions"]["joy"]
    assert joy["value"] < 65 and joy["undertone"]["value"] - joy["value"] > 5, "the mood outlasts the moment"


def test_only_the_dimensions_whose_curve_changed_are_reanchored(setup):
    mind, source, clock = setup
    policy(mind, source)
    first = anchors(migrated(mind))
    later(clock, hours=1)
    mind.record(event(mind, source, "spike", {"joy": 90}))
    spiked = raw(mind)
    held = anchors(spiked)
    assert {key for key in held if held[key] != first[key]} == {"joy"}
    assert held["joy"] == {"m": 60.0, "x": 90.0, "at": mind.clock(), "fp": L.fingerprint(spiked["dimensions"]["joy"])}
    later(clock, hours=1)
    mind.record(event(mind, source, "gloom", {"mood": 30}))
    assert anchors(raw(mind))["joy"] == held["joy"], "another dimension's event leaves joy's anchor alone"
    later(clock, hours=2)
    expected = L.undertone(spiked["dimensions"]["joy"], held["joy"], mind.clock())[0]
    mind.record(event(mind, source, "settled", {"joy": 50}))
    assert anchors(raw(mind))["joy"]["m"] == pytest.approx(expected, abs=1e-6), "from where the old curve had brought it"
    # A write that moves no curve anchors nothing, and the layer reads the same after it.
    before, view = raw(mind)[L.KEY], mind.read()
    mind.configure_contact({"command_id": "prefer", "agent_version": "synthetic-v1", "expected_revision": view["revision"],
                            "evidence_ids": [source("prefer")], "reason": "The owner prefers it", "wait_for_reply": False})
    assert raw(mind)[L.KEY] == before
    assert {k: d["undertone"] for k, d in mind.read()["dimensions"].items()} == {k: d["undertone"] for k, d in view["dimensions"].items()}


def test_every_writer_leaves_each_anchor_on_its_curve(setup, monkeypatch):
    """The hook sits where every writer saves, so the anchors follow the curves whichever path moved
    them: events, wishes retargeting initiative (legacy mode), a deferred draft, a resumed wish."""
    mind, source, clock = setup
    migrated(mind)

    def on_curve():
        state = raw(mind)
        assert all(anchors(state)[k]["fp"] == L.fingerprint(e) for k, e in state["dimensions"].items())
        assert all(d["undertone"]["status"] == "tracking" for d in mind.read()["dimensions"].values())

    initiative = anchors(raw(mind))["initiative"]
    wish(mind, source, "tea", strength=90, content="Tell her about the tea")
    assert anchors(raw(mind))["initiative"] != initiative, "the wish retargeted initiative"
    on_curve()
    later(clock, minutes=30)
    attempt = mind.claim_contact(owner_epoch="owner-1")
    mind.settle_contact(attempt_id=attempt["id"], state="canceled", reason="draft-decision",
                        decision={"action": "wait", "condition": "time", "reason": "Later", "retry_after_seconds": 600})
    on_curve()
    later(clock, minutes=11)
    assert mind.reconsider_contacts(owner_epoch="owner-1")["state"] == "resumed"
    on_curve()
    mind.record(event(mind, source, "an-evening", {key: 50 for key in ("joy", "fear", "closeness", "initiative")}))
    on_curve()


def test_replays_and_record_only_commits_do_not_reanchor(setup):
    mind, source, clock = setup
    migrated(mind)
    later(clock, hours=1)
    request = event(mind, source, "once", {"joy": 90, "fear": 40})
    first = mind.record(request)
    text = live(mind)
    later(clock, hours=1)
    assert mind.record(request) == first and live(mind) == text, "a replay writes nothing"

    def moves_a_curve(conn, state, event_id):
        state["dimensions"]["joy"].update(score=10, at=mind.clock())
        return {}

    request = {"command_id": "only-recorded", "agent_version": "synthetic-v1", "expected_revision": first["revision"],
               "evidence_ids": [source("only-recorded")], "reason": "Recorded, never applied"}
    assert mind._record_only(request, moves_a_curve)["revision"] == first["revision"]
    assert live(mind) == text


def test_the_echo_needs_a_strong_change_and_fades(setup):
    mind, source, clock = setup
    migrated(mind)
    mind.record(event(mind, source, "a-little-better", {"joy": 70}))
    assert raw(mind)[L.KEY]["echo"] is None and mind.read()["affect_layers"]["lingering"] is None
    mind.record(event(mind, source, "good-news", {"joy": 95, "closeness": 88}))
    echo = raw(mind)[L.KEY]["echo"]
    assert echo == {"at": mind.clock(), "moves": [["joy", 25.0], ["closeness", 13.0]]}
    assert not any(marker in json.dumps(echo) for marker in ("src_", "mem_", "reason"))
    shown = mind.read()["affect_layers"]["lingering"]
    assert shown == {"text": L.ECHOES["joy"][0], "dimension": "joy", "direction": "up", "since": echo["at"], "strength": 25.0}
    later(clock, hours=1)
    mind.record(event(mind, source, "small-thing", {"mood": 70}))
    assert raw(mind)[L.KEY]["echo"] == echo, "a weaker event leaves the last echo to fade on"
    later(clock, hours=2)
    assert mind.read()["affect_layers"]["lingering"]["strength"] == 6.2
    later(clock, minutes=30)
    assert mind.read()["affect_layers"]["lingering"] is None
    mind.record(event(mind, source, "let-down", {"joy": 35}))
    assert mind.read()["affect_layers"]["lingering"]["text"] == L.ECHOES["joy"][1]


def test_vitals_rise_with_arousal_follow_the_phase_and_stay_in_range():
    calm = {key: (spec["baseline"], spec["baseline"]) for key, spec in DIMENSIONS.items()}
    awake = {"status": "observing", "phase": "awake", "alertness": 60, "needs_review": False}
    noon = "2026-09-27T12:00:00+00:00"
    rest = L.vitals(calm, awake, noon, "UTC")
    assert rest == {"heart_rate_bpm": 72, "breaths_per_min": 14, "basis": "derived", "status": "current"}
    afraid = L.vitals({**calm, "fear": (60, 8)}, awake, noon, "UTC")
    assert afraid["heart_rate_bpm"] == 90 and afraid["breaths_per_min"] == 18
    settled = L.vitals({**calm, "contentment": (90, 60), "security": (95, 70)}, awake, noon, "UTC")
    assert settled["heart_rate_bpm"] < rest["heart_rate_bpm"]
    beats = [L.vitals(calm, {**awake, "phase": phase}, noon, "UTC")["heart_rate_bpm"]
             for phase in ("resting", "drowsy", "settling", "awake", "roused")]
    assert beats == sorted(beats) and beats[0] == 60 and beats[-1] == 76
    assert L.vitals(calm, {**awake, "alertness": 100}, noon, "UTC")["heart_rate_bpm"] == 78
    assert L.vitals(calm, awake, "2026-09-27T03:00:00+00:00", "UTC")["heart_rate_bpm"] == 65, "the small hour table"
    storm = {**calm, **{key: (100, DIMENSIONS[key]["baseline"]) for key in L.HEART}, "contentment": (0, 60), "security": (0, 70)}
    high = L.vitals(storm, {**awake, "phase": "roused", "alertness": 100}, noon, "UTC")
    assert (high["heart_rate_bpm"], high["breaths_per_min"]) == (130, 26)
    still = {**{key: (0, spec["baseline"]) for key, spec in DIMENSIONS.items()}, "contentment": (100, 60), "security": (100, 70)}
    low = L.vitals(still, {**awake, "phase": "resting", "alertness": 0}, "2026-09-27T02:00:00+00:00", "UTC")
    assert (low["heart_rate_bpm"], low["breaths_per_min"]) == (50, 8)
    # Without a usable rhythm its phase and alertness count for nothing, and the status says why.
    for rhythm, status in (({"status": "disabled", "mode": "interaction-led"}, "rhythm-disabled"),
                           ({"status": "forming", "phase": "forming"}, "rhythm-forming"),
                           ({**awake, "phase": "resting", "alertness": 5, "needs_review": True}, "rhythm-needs-review")):
        assert L.vitals(calm, rhythm, noon, "UTC") == {**rest, "status": status}
    assert L.vitals(calm, awake, noon, "Not/AZone") == L.vitals(calm, awake, noon, L.DEFAULT_TIMEZONE)


def test_the_view_shows_vitals_with_and_without_a_rhythm(setup):
    mind, source, _clock = setup
    migrated(mind)
    assert mind.read()["affect_layers"]["vitals"]["status"] == "rhythm-disabled"
    mind.configure_continuity(ContinuityConfig(command_id="rhythm-on", agent_version="synthetic-v1",
        expected_revision=mind.read()["revision"], evidence_ids=[source("rhythm-on")], features={"rhythm": True}, reason="test"))
    assert mind.read()["affect_layers"]["vitals"]["status"] == "rhythm-forming"
    calm = mind.read()["affect_layers"]["vitals"]["heart_rate_bpm"]
    mind.record(AffectiveEvent(command_id="bedtime", agent_version="synthetic-v1", expected_revision=mind.read()["revision"],
        evidence_ids=[source("bedtime")], reason="Winding down",
        rhythm=RhythmProposal(phase="resting", alertness=20, target=10, half_life_minutes=180, reason="Sleepy")))
    view = mind.read()
    assert view["affect_layers"]["vitals"]["status"] == "current"
    assert view["affect_layers"]["vitals"]["heart_rate_bpm"] == max(50, calm - 12 - 6), "the resting phase and low alertness respect the pulse floor, also at night"


def test_feeling_words_come_from_a_fixed_table():
    calm = {key: (spec["baseline"], spec["baseline"]) for key, spec in DIMENSIONS.items()}

    def feel(**moved):
        levels = {**calm, **{key: (DIMENSIONS[key]["baseline"] + shift, DIMENSIONS[key]["baseline"]) for key, shift in moved.items()}}
        return L.words(levels, *L.FEELING_WORDS)

    assert feel() == {"text": "平静", "dimensions": []}
    assert feel(joy=30) == {"text": "雀跃", "dimensions": ["joy"]}
    assert feel(joy=15, longing=13) == {"text": "开心、想念", "dimensions": ["joy", "longing"]}
    assert feel(jealousy=14)["text"] == "有点酸" and feel(irritability=30)["text"] == "心烦"
    assert feel(fear=14)["text"] == "不安" and feel(security=-30)["text"] == "不安" and feel(fear=40)["text"] == "害怕"
    assert feel(mood=-20)["text"] == "有点闷" and feel(contentment=15)["text"] == "踏实" and feel(wonder=20)["text"] == "好奇"
    assert feel(sadness=40, joy=-13, grievance=20)["text"] == "难过、有点委屈", "the two strongest, strongest first"
    assert feel(joy=11, longing=11) == {"text": "平静", "dimensions": []}, "below the mild threshold nothing is named"
    assert feel(sadness=40, grievance=20) == feel(grievance=20, sadness=40)
    named = {word for mild, strong, _ in L.FEELINGS for word in (mild, strong)}
    assert len(named) == 2 * len(L.FEELINGS) and all(key in DIMENSIONS for *_, signals in L.FEELINGS for key, _, _ in signals)
    assert set(L.ECHOES) == set(DIMENSIONS) and all(len(pair) == 2 and all(pair) for pair in L.ECHOES.values())


def test_forming_until_the_migration_and_continuous_across_it(setup):
    mind, source, clock = setup
    mind.record(event(mind, source, "before-the-layers", {"joy": 90}))
    view = mind.read()
    assert view["dimensions"]["joy"]["undertone"] == {"value": 90.0, "status": "forming"}
    assert view["affect_layers"]["undertone"]["status"] == "forming" and view["affect_layers"]["lingering"] is None
    assert L.KEY not in raw(mind), "no block and no echo before the migration"
    later(clock, hours=1)
    forming = mind.read()["dimensions"]
    moved = mind.ensure_affect_layers(agent_version="synthetic-v2")
    assert moved["state"] == "added" and moved["revision"] == view["revision"] + 1
    after = mind.read()["dimensions"]
    assert all(after[k]["undertone"] == {"value": forming[k]["undertone"]["value"], "status": "tracking"} for k in after)
    assert mind.ensure_affect_layers(agent_version="synthetic-v2") == {"state": "unchanged"}
    assert mind.read()["revision"] == moved["revision"]


def test_the_migration_is_one_revision_and_history_rebuilds_with_patches_on(setup):
    from test_history_layer import evolve, revert
    from test_history_patches import drive, patches, rebuilds, rows

    from kin_mind.history import is_patch
    mind, source, clock = setup
    changed = evolve(mind, source, clock)
    patches(mind)
    texts = drive(mind, source, clock, rounds=3)
    moved = mind.ensure_affect_layers(agent_version="synthetic-v2")
    texts[moved["revision"]] = live(mind)
    texts |= drive(mind, source, clock, rounds=3, start=3)
    stored = rows(mind)
    assert is_patch(stored[moved["revision"]]) and is_patch(stored[max(stored)])
    with mind.engine.db.connect() as conn:
        kinds = [row[0] for row in conn.execute("SELECT kind FROM mind_events WHERE scope=? ORDER BY revision", (mind.scope.key(),))]
    assert kinds.count("affect-layers-added") == 1
    rebuilds(mind, texts)
    assert mind.ensure_affect_layers(agent_version="synthetic-v2") == {"state": "unchanged"}
    # A reversion of an evolution from before the layers keeps them, every anchor on its new curve.
    reverted = revert(mind, source, clock, changed["event_id"])
    state = raw(mind)
    assert state["revision"] == reverted["revision"] and set(anchors(state)) == set(state["dimensions"])
    assert all(anchors(state)[k]["fp"] == L.fingerprint(e) for k, e in state["dimensions"].items())
    assert anchors(state)["curiosity"]["at"] == state["dimensions"]["curiosity"]["at"]
    texts[reverted["revision"]] = live(mind)
    rebuilds(mind, texts)


def test_a_history_patch_carries_only_the_anchors_that_moved(setup):
    """The history layer patches two keys deep, so each anchor is a key of the block itself: a revision
    that moved one curve stores one anchor, not the other twenty-seven beside it."""
    from test_history_patches import patches, rows

    from kin_mind.history import is_patch
    assert not set(L.META) & set(DIMENSIONS)
    mind, source, clock = setup
    policy(mind, source)
    migrated(mind)
    patches(mind)
    later(clock, hours=1)
    revision = mind.record(event(mind, source, "a-little-lift", {"joy": 70}))["revision"]
    row = rows(mind)[revision]
    assert is_patch(row) and [op[1] for op in row["patch"] if op[1][0] == L.KEY] == [[L.KEY, "joy"]]
    later(clock, hours=1)
    revision = mind.record(event(mind, source, "a-fright", {"fear": 60}))["revision"]
    assert [op[1] for op in rows(mind)[revision]["patch"] if op[1][0] == L.KEY] == [[L.KEY, "echo"], [L.KEY, "fear"]]


def test_a_curve_moved_behind_the_hook_is_estimated_the_same_before_and_after_the_next_save(setup):
    mind, source, clock = setup
    migrated(mind)
    later(clock, hours=1)
    with mind.engine.db.connect(write=True) as conn:
        # A writer that bypasses Mind._save, as a release rolled back would.
        state = json.loads(conn.execute("SELECT data FROM mind_state WHERE scope=?", (mind.scope.key(),)).fetchone()[0])
        state["dimensions"]["fear"].update(score=70, at=mind.clock())
        conn.execute("UPDATE mind_state SET data=? WHERE scope=?", (json.dumps(state), mind.scope.key()))
    later(clock, hours=2)
    fear = mind.read()["dimensions"]["fear"]["undertone"]
    assert fear["status"] == "estimated" and 8 < fear["value"] < 70
    mind.record(event(mind, source, "unrelated", {"joy": 70}))
    again = mind.read()["dimensions"]["fear"]["undertone"]
    assert again == {"value": fear["value"], "status": "tracking"}


def test_no_source_or_record_id_enters_the_derived_layers(setup):
    from kin_mind.context import Contexts
    mind, source, _clock = setup
    migrated(mind)
    mind.record(event(mind, source, "vivid", {"joy": 95, "fear": 60, "longing": 80}))
    view = mind.read()
    shown = [view["affect_layers"], raw(mind)[L.KEY], [d["undertone"] for d in view["dimensions"].values()],
             Contexts(mind).affective().get("affect_layers")]
    assert shown[-1] and all(marker not in json.dumps(part) for part in shown for marker in ("src_", "mem_"))


def test_the_view_and_every_projection_of_it_carry_the_layers(setup):
    from kin_mind.appraisal import appraisal_context
    from kin_mind.context import Contexts, affect_projection
    from kin_mind.expression import compile_expression
    mind, source, _clock = setup
    migrated(mind)
    mind.configure_continuity(ContinuityConfig(command_id="all-on", agent_version="synthetic-v1",
        expected_revision=mind.read()["revision"], evidence_ids=[source("all-on")],
        features={"interpretation": True, "concerns": True, "expression": True, "rhythm": True}, reason="test"))
    mind.record(AffectiveEvent(command_id="evening", agent_version="synthetic-v1", expected_revision=mind.read()["revision"],
        evidence_ids=[source("evening")], values={"joy": 92, "longing": 80}, reason="A warm evening",
        rhythm=RhythmProposal(phase="settling", alertness=55, target=40, half_life_minutes=60, reason="Evening")))
    view = mind.read()
    layers = view["affect_layers"]
    assert set(layers) == {"version", "basis", "undertone", "feeling", "lingering", "vitals"} and layers["version"] == L.VERSION
    assert layers["feeling"] == {"text": "好想念、雀跃", "dimensions": ["longing", "joy"]}
    assert layers["lingering"]["dimension"] == "longing" and layers["vitals"]["status"] == "current"
    assert all(set(d["undertone"]) == {"value", "status"} for d in view["dimensions"].values())

    context = appraisal_context({"state": view, "new_evidence": []})
    assert context["context_projection"] == "affect-decision-v4" and context["state"]["affect_layers"] == layers
    assert context["state"]["dimensions"]["joy"]["undertone"] == view["dimensions"]["joy"]["undertone"]["value"]

    state = Contexts(mind).affective()
    assert state["rhythm"]["phase"] == "settling" and state["rhythm"]["alertness"] == view["rhythm"]["alertness"]
    assert state["dimensions"]["joy"]["undertone"] == view["dimensions"]["joy"]["undertone"]["value"]
    assert state["affect_layers"] == L.compact(layers) and state["affect_layers"]["feeling"] == "好想念、雀跃"

    item = affect_projection(view)["affect_layers"]
    assert item["vitals"]["heart_rate_bpm"] % 5 == 0 and item["vitals"]["breaths_per_min"] % 2 == 0
    assert item["lingering"] == L.ECHOES["longing"][0] and item["feeling"] == "好想念、雀跃"
    assert "affect_layers" not in affect_projection({**view, "continuity": {**view["continuity"], "activation": "shadow"}})

    # Shown beside the expression, never fed into it: the hints are what the scores alone give.
    bare = {k: {f: v for f, v in d.items() if f != "undertone"} for k, d in view["dimensions"].items()}
    assert compile_expression(view["dimensions"], rhythm=view["rhythm"]) == compile_expression(bare, rhythm=view["rhythm"])
    alone = compile_expression(bare, rhythm=view["rhythm"], config_version=view["continuity"]["version"],
                               persona=view["persona_contract"])
    assert view["expression"]["guidance"] == alone["guidance"] and view["expression"]["version"] == alone["version"]


def test_the_affect_item_revision_holds_within_the_rounding_band(setup):
    from kin_mind.context import affect_projection
    block = {"undertone": {"status": "tracking", "text": "平静", "leaning": {}}, "feeling": {"text": "开心"}, "lingering": None,
             "vitals": {"heart_rate_bpm": 71, "breaths_per_min": 15, "basis": "derived", "status": "current"}}
    nudged = deepcopy(block)
    nudged["vitals"].update(heart_rate_bpm=72, breaths_per_min=16)
    assert L.compact(block, coarse=True) == L.compact(nudged, coarse=True)
    assert L.compact(block) != L.compact(nudged), "the state read keeps the exact pulse"
    nudged["vitals"]["heart_rate_bpm"] = 74
    assert L.compact(block, coarse=True) != L.compact(nudged, coarse=True)

    mind, source, clock = setup
    migrated(mind)
    # Forward to ten past the next hour, so the three reads below share one hour of the pulse's table
    # (a clock moved back behind the fixture's evidence would mark every score for review instead).
    clock[0] = (clock[0] + timedelta(hours=1)).replace(minute=10)

    def revision():
        return digest(affect_projection(mind.read()))

    first = revision()
    for _ in range(3):
        later(clock, minutes=7)
        assert revision() == first, "at rest the item says the same thing all hour"
    mind.record(event(mind, source, "startled", {"fear": 70}))
    assert revision() != first


def test_nothing_here_calls_a_model(setup, monkeypatch):
    import httpx

    from kin_mind import appraisal
    from kin_mind.context import Contexts

    def refuse(*args, **kwargs):
        raise AssertionError("no model call in the derived layers")

    for owner, name in ((httpx.Client, "send"), (httpx.AsyncClient, "send"), (appraisal.DeepSeek, "appraise"),
                        (appraisal.DeepSeek, "structured")):
        monkeypatch.setattr(owner, name, refuse)
    source_text = inspect.getsource(L)
    assert not any(word in source_text for word in ("httpx", "appraisal", "DeepSeek", "model_lanes", "provider"))
    mind, source, clock = setup
    migrated(mind)
    mind.record(event(mind, source, "felt", {"joy": 95, "fear": 50}))
    later(clock, hours=2)
    assert mind.read()["affect_layers"]["lingering"]
    assert Contexts(mind).affective()["affect_layers"]["vitals"]["basis"] == "derived"


def test_an_assessment_commit_reanchors_and_leaves_an_echo(setup):
    from test_kin_mind import FakeReviewer

    from kin_mind.appraisal import Appraisal, Appraisals
    mind, source, clock = setup
    migrated(mind)
    later(clock, hours=1)
    jobs = Appraisals(mind)
    jobs.enqueue([source("owner-news")], "synthetic-v1")
    assert jobs.run_one(FakeReviewer(Appraisal(values={"joy": 88, "anticipation": 70}, reason="Good news")))["state"] == "complete"
    state = raw(mind)
    assert anchors(state)["joy"]["at"] == mind.clock() and anchors(state)["joy"]["x"] == 88.0
    assert state[L.KEY]["echo"]["moves"][:2] == [["anticipation", 30.0], ["joy", 28.0]]


def test_the_motivation_drive_is_followed_across_its_end_through_the_mind(setup):
    mind, source, clock = setup
    migrated(mind)
    mind.record(AffectiveEvent(command_id="restless", agent_version="synthetic-v1", expected_revision=mind.read()["revision"],
        evidence_ids=[source("restless")], values={"curiosity": 90}, reason="A live question",
        motivations={"curiosity": Motivation(target=95, half_life_minutes=30, reason="Wants to look it up")}))
    state = raw(mind)
    entry, anchor = state["dimensions"]["curiosity"], anchors(state)["curiosity"]
    start = datetime.fromisoformat(mind.clock())
    m, reached = anchor["m"], 0.0
    for hours in (1, 2, 2.5, 6):
        clock[0] = start + timedelta(hours=hours)
        steps = round((hours - reached) / 0.01)

        def x(h):
            return project(entry, (start + timedelta(hours=h)).isoformat())
        for index in range(steps):
            h = reached + index * 0.01
            k1 = (x(h) - m) / L.TAU_HOURS
            k2 = (x(h + 0.005) - (m + 0.005 * k1)) / L.TAU_HOURS
            k3 = (x(h + 0.005) - (m + 0.005 * k2)) / L.TAU_HOURS
            k4 = (x(h + 0.01) - (m + 0.01 * k3)) / L.TAU_HOURS
            m += 0.01 / 6 * (k1 + 2 * k2 + 2 * k3 + k4)
        reached = hours
        assert mind.read()["dimensions"]["curiosity"]["undertone"]["value"] == pytest.approx(m, abs=0.051)
