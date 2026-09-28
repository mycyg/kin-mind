"""Evidence references keep what their readers read, and never their source's metadata (the owner's
decision, 2026-09-28: 来源的元数据只做为索引检索，不应该注入哈，也不必要引用，只需要可以溯源就行).

What is asserted here: once `slim-evidence-refs` has marked the document, a reference written into any
section of it is kept to `TRACE`, and so is one that moves to either archive; nothing a model is shown
-- the read, its history snapshots, the interaction projection, the wishes a contact attempt keeps --
carries a source's metadata, marked or not; freshness and the conflict checks read slim references as
they read full ones; an erase still takes everything derived from a deleted source; and the migration
reads by default, applies as one recorded revision, applies once, and undoes exactly -- the document
and both archive tables byte for byte, apart from the revision and its time.

Synthetic replays only: an injected clock, sources through the engine, no model and no network."""
import json
from datetime import timedelta

import pytest
from test_erasure import texts_everywhere
from test_exploration_decision_archive import decided

from eventmem.core.db import dumps
from eventmem.core.models import RevisionInput, SourceInput
from kin_mind import desire_archive, evidence_refs
from kin_mind import exploration_decision_archive as decisions
from kin_mind.actions import ActionEvents
from kin_mind.continuity import ConcernChange, ContinuityConfig, evidence_key
from kin_mind.erasure import ERASED
from kin_mind.host import dispatch
from kin_mind.interaction_projection import interaction_projection
from kin_mind.memory import MemoryContinuity
from kin_mind.state import AffectiveEvent, DesireChange

pytest_plugins = ("test_kin_mind", "test_autonomous_plans")

AGENT = "synthetic-v1"
MARKER = "zorbquill"
DROPPED = set(evidence_refs.DROPPED)


# --- looking at references ---------------------------------------------------------------------------

def refs_in(value, path=()):
    """Every dict that names a source, a record and a hash, wherever it sits, with its path."""
    if isinstance(value, dict):
        if all(isinstance(value.get(k), str) for k in ("source_id", "record_id", "hash")):
            yield path, value
            return
        for key, item in value.items():
            yield from refs_in(item, (*path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from refs_in(item, (*path, index))


def carrying(value):
    """The references in `value` that still carry anything the trace drops."""
    return [path for path, ref in refs_in(value) if DROPPED & set(ref) and not ref.get("erased")]


def stored(mind):
    with mind.engine.db.connect() as conn:
        row = conn.execute("SELECT revision,data FROM mind_state WHERE scope=?", (mind.scope.key(),)).fetchone()
        return row["revision"], row["data"]


def table(mind, name):
    with mind.engine.db.connect() as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone():
            return {}
        return {row[0]: row[1] for row in conn.execute(f"SELECT id,data FROM {name} WHERE scope=? ORDER BY id",
                                                       (mind.scope.key(),))}


def originals(mind):
    with mind.engine.db.connect() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {evidence_refs.TABLE} WHERE scope=?", (mind.scope.key(),)).fetchone()[0]


def kinds(mind):
    with mind.engine.db.connect() as conn:
        return [row[0] for row in conn.execute("SELECT kind FROM mind_events WHERE scope=? ORDER BY revision",
                                               (mind.scope.key(),))]


def rich(mind, clock, key, words=None):
    """An owner message whose source carries metadata with ids and words of its own, as a host
    message does: what a reference used to copy."""
    return mind.engine.receive(SourceInput(
        namespace="test", key=key, scope=mind.scope, authority="explicit", occurred_at=clock[0].isoformat(),
        text=words or f"synthetic message {key}",
        metadata={"role": "user", "host_event": "message", "channel": "wechat", "title": f"{MARKER} title {key}",
                  "runtime_event_id": "runtime-" + key}))["id"]


def create(mind, clock, key, evidence, kind="contact", **fields):
    clock[0] += timedelta(minutes=1)
    fields = {"content": "say " + key, "topic": "topic " + key, "reason": "worth it " + key, **fields}
    return mind.manage_desire(DesireChange(
        command_id=key, agent_version=AGENT, expected_revision=mind.read()["revision"], evidence_ids=evidence,
        action="create", kind=kind, strength=70, expires_at=(clock[0] + timedelta(days=3)).isoformat(),
        completion="she has it", **fields))["desire_id"]


def every_section(mind, clock):
    """References written into every section of the document that keeps them, by the writers that
    write them, and by `_save` for the sections only an appraisal's commit writes."""
    mind.record(AffectiveEvent(command_id="felt", agent_version=AGENT, expected_revision=mind.read()["revision"],
                               evidence_ids=[rich(mind, clock, "felt")], values={"joy": 70}, reason="A sourced event"))
    create(mind, clock, "wish", [rich(mind, clock, "wish")])
    mind.configure_continuity(ContinuityConfig(command_id="continuity", agent_version=AGENT,
                                               expected_revision=mind.read()["revision"],
                                               evidence_ids=[rich(mind, clock, "continuity")],
                                               features={"concerns": True, "interpretation": True, "rhythm": True},
                                               reason="synthetic"))
    worry = rich(mind, clock, "worry")
    concern = mind.manage_concern(ConcernChange(
        command_id="c1", agent_version=AGENT, expected_revision=mind.read()["revision"], evidence_ids=[worry],
        action="create", key="exam", kind="care", content="her exam", topic="exam", intensity=60, basis="explicit",
        confidence=0.9, reason="she is worried"))["concern_id"]
    mind.manage_concern(ConcernChange(
        command_id="c2", agent_version=AGENT, expected_revision=mind.read()["revision"],
        evidence_ids=[rich(mind, clock, "passed")], action="resolve", concern_id=concern, reason="she passed"))
    mind.configure_contact({"command_id": "contact", "agent_version": AGENT, "expected_revision": mind.read()["revision"],
                            "evidence_ids": [rich(mind, clock, "contact")], "reason": "her word", "wait_for_reply": False})
    mind.configure_behavior({"command_id": "style", "agent_version": AGENT, "expected_revision": mind.read()["revision"],
                             "evidence_ids": [rich(mind, clock, "style")], "reason": "her word", "style": "contextual"})
    mind.configure_autonomy({"command_id": "autonomy", "agent_version": AGENT, "expected_revision": mind.read()["revision"],
                             "evidence_ids": [rich(mind, clock, "autonomy")], "reason": "her word"})
    mind.configure_contact_frequency({"command_id": "frequency", "agent_version": AGENT,
                                      "expected_revision": mind.read()["revision"],
                                      "evidence_ids": [rich(mind, clock, "frequency", "once a day is enough")],
                                      "reason": "her word"})
    ActionEvents(mind).configure({"command_id": "actions", "agent_version": AGENT, "expected_revision": mind.read()["revision"],
                                  "evidence_ids": [rich(mind, clock, "actions")], "reason": "her word"})
    decided(mind, clock, "explore_slim")
    said = rich(mind, clock, "assessed")

    def assessed(conn, state, event_id):
        refs = mind._evidence(conn, [said])
        state["last_assessment"] = {"event_id": event_id, "evidence": refs,
                                    "understanding": {"topic": "exam", "evidence": mind._evidence(conn, [said])}}
        state["rhythm"] = {"phase": "awake", "alertness": 60, "target": 50, "half_life_minutes": 60,
                           "reason": "a sourced rhythm", "evidence": refs, "at": mind.clock(), "event_id": event_id,
                           "config_version": state["continuity"]["version"]}
    mind._mutate({"command_id": "assessed", "agent_version": AGENT, "expected_revision": mind.read()["revision"]},
                 "test-assessment", assessed)


SECTIONS = {"dimensions", "desires", "continuity", "concerns", "contact_preference", "behavior", "autonomy",
            "contact_frequency", "action_policy", "exploration_decisions", "last_assessment", "rhythm"}


# --- written slim ------------------------------------------------------------------------------------

def test_once_marked_every_reference_any_section_is_given_keeps_only_what_its_readers_read(setup):
    mind, _, clock = setup
    assert evidence_refs.run(mind, apply=True)["marked"] is True
    every_section(mind, clock)
    revision, text = stored(mind)
    state = json.loads(text)
    assert state[evidence_refs.MARK] == evidence_refs.SHAPE
    found = list(refs_in(state))
    assert {path[0] for path, _ in found} >= SECTIONS, SECTIONS - {path[0] for path, _ in found}
    assert any("resolution_evidence" in path for path, _ in found)
    assert any(path[:3] == ("last_assessment", "understanding", "evidence") for path, _ in found)
    assert not carrying(state) and MARKER not in text and "runtime-" not in text
    with mind.engine.db.connect() as conn:
        for _, ref in found:
            # Exactly the trace of the reference the writer was given, and still current.
            assert ref == evidence_refs.trace(mind._reference(conn, ref["source_id"], ref["record_id"]))
            assert set(ref) == set(evidence_refs.TRACE) and mind._fresh(conn, [ref])
    # The history row of every revision since is the document it wrote.
    with mind.engine.db.connect() as conn:
        from kin_mind.history import materialize
        assert dumps(materialize(conn, mind.scope.key(), revision)) == text


def test_unmarked_the_document_is_written_exactly_as_before(setup):
    mind, _, clock = setup
    every_section(mind, clock)
    state = json.loads(stored(mind)[1])
    assert evidence_refs.MARK not in state
    full = [ref for _, ref in refs_in(state)]
    assert full and all({"metadata", "authority", "session", "received_at"} <= set(ref) for ref in full)


def test_a_wish_and_a_decision_that_move_to_their_archives_move_slim_and_come_back_slim(setup):
    mind, _, clock = setup
    MemoryContinuity(mind).configure({"desire_archive": True, "exploration_decision_archive": True})
    evidence_refs.run(mind, apply=True)
    early = create(mind, clock, "early", [rich(mind, clock, "early")])
    mind.manage_desire(DesireChange(command_id="done", agent_version=AGENT, expected_revision=mind.read()["revision"],
                                    evidence_ids=[rich(mind, clock, "done")], action="complete", desire_id=early,
                                    reason="said"))
    for index in range(14):
        decided(mind, clock, f"explore_move{index:02d}")
    clock[0] += timedelta(days=10)
    create(mind, clock, "late", [rich(mind, clock, "late")])
    assert desire_archive.archive(mind, apply=True, keep=1)["moved_count"] == 1
    assert decisions.archive(mind, apply=True)["moved_count"] >= 1
    for name in ("mind_desire_archive", "mind_exploration_decision_archive"):
        rows = table(mind, name)
        assert rows and not any(carrying(json.loads(data)) for data in rows.values()), name
        assert all(refs_in(json.loads(data)) for data in rows.values())
    # Put back, they are what the document keeps.
    desire_archive.restore(mind, apply=True)
    decisions.restore(mind, apply=True)
    state = json.loads(stored(mind)[1])
    assert early in state["desires"] and not carrying(state)


# --- shown slim, marked or not -----------------------------------------------------------------------

@pytest.mark.parametrize("marked", [False, True])
def test_no_read_a_model_is_shown_carries_a_sources_metadata(setup, marked):
    mind, _, clock = setup
    every_section(mind, clock)
    if marked:
        evidence_refs.run(mind, apply=True)
        create(mind, clock, "after", [rich(mind, clock, "after")])
    view = mind.read(history=40, query="exam")
    assert view["history"] and any(entry["snapshot"] for entry in view["history"])
    assert list(refs_in(view)), "the read still names what it rests on"
    assert not carrying(view) and MARKER not in dumps(view) and "runtime-" not in dumps(view)
    projection = interaction_projection(mind.read())
    assert projection["desires"] and projection["desires"][0]["evidence"]
    assert not carrying(projection) and MARKER not in dumps(projection)
    # The host's read, its projection for a draft, and the MCP read of the state.
    config = {"root": str(mind.engine.db.root), "scope": mind.scope.model_dump(), "agent_version": AGENT,
              "session_id": "synthetic-session"}
    for request in ({"history": 5}, {"projection": "interaction"}):
        answer = dispatch(config, "read", request)
        assert not carrying(answer) and MARKER not in dumps(answer), request
    from kin_mind.mcp import register_mind_tools

    class Server:
        def __init__(self):
            self.tools = {}

        def tool(self):
            def register(function):
                self.tools[function.__name__] = function
                return function
            return register
    server = Server()
    register_mind_tools(server, mind.engine)
    for arguments in ({"history": 3}, {"query": "exam"}):
        answer = server.tools["read_affective_state"](scope=mind.scope, **arguments)
        assert not carrying(answer) and MARKER not in dumps(answer), arguments
    # The view the contact paths read the wishes from.
    with mind.engine.db.connect() as conn:
        offered = mind._view(conn, mind._load(conn), mind.clock())
    assert offered["desires"] and not carrying(offered) and MARKER not in dumps(offered)


def test_the_appraisal_context_shows_plans_without_their_sources_metadata_and_new_evidence_as_it_was(setup):
    """The projection an assessment is shown keeps a plan whole (`compact_plan`), its evidence
    references with it: they name their source and record only. What is under review is not a
    reference -- its source's metadata is what the prompt reads its exploration id from -- and stays."""
    from kin_mind.appraisal import appraisal_context
    from kin_mind.plans import AutonomousPlans
    mind, _, clock = setup
    MemoryContinuity(mind).configure({"autonomous_plans": True})
    AutonomousPlans(mind).manage({"command_id": "create:sea", "action": "create", "key": "sea", "goal": "看海",
                                  "motivation": "一起的计划", "reason": "有来源的计划", "evidence_ids": [rich(mind, clock, "plan")],
                                  "steps": [{"id": "list", "actor": "create", "goal": "列清单", "completion": "清单保存"}]})
    plans = AutonomousPlans(mind).read(limit=40)
    assert carrying(plans), "the plan row keeps its full references"
    new_evidence = [{"id": "src_" + "a" * 32, "authority": "model", "revision": 1, "text": "a result",
                     "metadata": {"host_event": "exploration-result", "exploration_id": "explore_sea"}}]
    context = {"state": mind.read(), "new_evidence": new_evidence, "stimulus": "exploration-result",
               "autonomy_context": {"semantic_actions": True, "plans": plans}}
    shown = appraisal_context(context)
    assert shown["autonomy_context"]["plans"]["plans"][0]["evidence"]
    assert not carrying(shown) and MARKER not in dumps(shown)
    assert shown["new_evidence"][0]["metadata"]["exploration_id"] == "explore_sea"


def test_a_contact_attempt_offers_and_keeps_its_wishes_without_their_sources_metadata(setup):
    mind, _, clock = setup
    wish = create(mind, clock, "offered", [rich(mind, clock, "offered")])
    candidate = mind.contact_candidate()
    assert candidate["eligible"] and not carrying(candidate) and MARKER not in dumps(candidate)
    attempt = mind.claim_contact(owner_epoch="owner-1")
    assert [d["id"] for d in attempt["desires"]] == [wish] and attempt["desires"][0]["evidence"]
    assert not carrying(attempt) and MARKER not in dumps(attempt)
    with mind.engine.db.connect() as conn:
        kept = conn.execute("SELECT data FROM mind_contacts WHERE id=?", (attempt["id"],)).fetchone()[0]
    assert MARKER not in kept
    # What a settled draft names it was handed still names the wish's own source and record.
    mind.settle_contact(attempt_id=attempt["id"], state="canceled", reason="draft-decision", shown_ids=[],
                        decision={"action": "wait", "condition": "time", "reason": "later", "retry_after_seconds": 600})
    with mind.engine.db.connect() as conn:
        named = json.loads(conn.execute("SELECT data FROM mind_contacts WHERE id=?", (attempt["id"],)).fetchone()[0])
    ref = attempt["desires"][0]["evidence"][0]
    assert {ref["source_id"], ref["record_id"]} <= set(named["evaluated_ids"])


# --- read as before ----------------------------------------------------------------------------------

def test_freshness_and_the_conflict_checks_read_a_slim_reference_as_they_read_a_full_one(setup):
    mind, _, clock = setup
    evidence_refs.run(mind, apply=True)
    said = rich(mind, clock, "fresh")
    wish = create(mind, clock, "fresh", [said])
    stored_ref = json.loads(stored(mind)[1])["desires"][wish]["evidence"][0]
    with mind.engine.db.connect() as conn:
        full = mind._reference(conn, said, stored_ref["record_id"])
        assert mind._fresh(conn, [stored_ref]) and evidence_key(stored_ref) == evidence_key(full)
    view = mind.read()
    assert not next(d for d in view["desires"] if d["id"] == wish)["needs_review"]
    assert mind.contact_candidate()["eligible"] is True
    # The owner corrects what the wish rests on: the slim reference is no longer current.
    mind.engine.revise(stored_ref["record_id"], RevisionInput(expected_revision=1, command_id="correct",
                                                              action="correct", content="corrected words",
                                                              reason="owner correction"))
    with mind.engine.db.connect() as conn:
        assert not mind._fresh(conn, [stored_ref])
    assert next(d for d in mind.read()["desires"] if d["id"] == wish)["needs_review"]
    # A newer source under the same namespace and key replaces it too.
    other = rich(mind, clock, "replaced")
    other_wish = create(mind, clock, "replaced", [other])
    clock[0] += timedelta(minutes=1)
    mind.engine.receive(SourceInput(namespace="test", key="replaced", version="2", scope=mind.scope,
                                    authority="explicit", occurred_at=clock[0].isoformat(), text="newer words",
                                    metadata={"role": "user", "host_event": "message"}))
    assert next(d for d in mind.read()["desires"] if d["id"] == other_wish)["needs_review"]


def test_a_decision_reconsidered_on_slim_references_still_needs_new_evidence(setup):
    """The exploration decision's reconsideration compares (source, hash) of what it rested on with
    what a new proposal cites: on slim references exactly as on full ones."""
    from kin_mind.exploration_decisions import require_share
    mind, _, clock = setup
    evidence_refs.run(mind, apply=True)
    decided(mind, clock, "explore_share", "share")
    state = json.loads(stored(mind)[1])
    kept = state["exploration_decisions"]["explore_share"]
    assert not carrying(kept) and kept["evidence"]
    with mind.engine.db.connect() as conn:
        assert require_share(mind, conn, state, "explore_share")["revision"] == 1


# --- erasure -----------------------------------------------------------------------------------------

def test_an_erase_still_takes_everything_derived_from_a_deleted_source(setup):
    mind, _, clock = setup
    MemoryContinuity(mind).configure({"desire_archive": True})
    secret = rich(mind, clock, "secret", f"she said {MARKER}-secret")
    moved = create(mind, clock, "moved", [secret])
    mind.manage_desire(DesireChange(command_id="moved-done", agent_version=AGENT, expected_revision=mind.read()["revision"],
                                    evidence_ids=[secret, rich(mind, clock, "moved-done")], action="complete",
                                    desire_id=moved, reason=f"said {MARKER}-secret"))
    kept_wish = create(mind, clock, "kept", [secret], content=f"say {MARKER}-secret")
    evidence_refs.run(mind, apply=True)
    create(mind, clock, "newest", [rich(mind, clock, "newest")])
    desire_archive.archive(mind, apply=True, days=30, keep=2)
    assert moved in table(mind, "mind_desire_archive")
    mind.engine.delete(secret)
    state = json.loads(stored(mind)[1])
    wish = state["desires"][kept_wish]
    assert wish["content"] == ERASED and all(ref.get("erased") for ref in wish["evidence"])
    archived = json.loads(table(mind, "mind_desire_archive")[moved])
    assert all(ref.get("erased") for ref in archived["evidence"] if ref["source_id"] == secret)
    assert archived["reason"] == ERASED
    # Found by its id alone, in the document and in the archive: no copy of its words is left there.
    assert f"{MARKER}-secret" not in stored(mind)[1] and f"{MARKER}-secret" not in dumps(archived)


def test_the_originals_kept_for_an_undo_are_reached_by_an_erase(setup):
    mind, _, clock = setup
    said = rich(mind, clock, "blanked")
    wish = create(mind, clock, "blanked", [said])
    with mind.engine.db.connect(write=True) as conn:
        state = mind._load(conn)
        # What an earlier erase left: the copy's words blanked, the source row's kept.
        state["desires"][wish]["evidence"][0]["metadata"]["title"] = ERASED
        mind._save(conn, state)
    report = evidence_refs.run(mind, apply=True)
    assert report["originals_kept"] >= 1 and originals(mind) >= 1
    mind.engine.delete(said)
    assert not {name for name, _ in texts_everywhere(mind.engine, f"{MARKER} title blanked")} - {"mind_events"}


# --- the migration -----------------------------------------------------------------------------------

def legacy(mind, clock):
    """A store as the release before this one leaves it: references everywhere, in the document and
    in both archives, one written before `received_at` was kept, one whose copied words an erase had
    blanked, a tombstone, and one whose source is gone from under it."""
    MemoryContinuity(mind).configure({"desire_archive": True, "exploration_decision_archive": True})
    every_section(mind, clock)
    old = create(mind, clock, "old-wish", [rich(mind, clock, "old-wish")])
    mind.manage_desire(DesireChange(command_id="old-done", agent_version=AGENT, expected_revision=mind.read()["revision"],
                                    evidence_ids=[rich(mind, clock, "old-done")], action="complete", desire_id=old,
                                    reason="said"))
    for index in range(14):
        decided(mind, clock, f"explore_old{index:02d}")
    clock[0] += timedelta(days=10)
    for key in ("legacy", "blanked", "gone", "tomb"):
        create(mind, clock, key, [rich(mind, clock, key)])
    desire_archive.archive(mind, apply=True, keep=10)
    decisions.archive(mind, apply=True)
    gone_source = None
    with mind.engine.db.connect(write=True) as conn:
        state = mind._load(conn)
        for desire in state["desires"].values():
            topic, ref = desire["topic"], desire["evidence"][0]
            if topic == "topic legacy":
                ref.pop("received_at")
            elif topic == "topic blanked":
                ref["metadata"]["title"] = ERASED
            elif topic == "topic tomb":
                desire["evidence"][0] = {**{k: ref[k] for k in ("source_id", "record_id", "hash", "revision", "authority")},
                                         "erased": True}
            elif topic == "topic gone":
                gone_source = ref["source_id"]
        mind._save(conn, state)
        # A source row gone from under a reference an erase never reached.
        conn.execute("UPDATE sources SET deleted=1 WHERE id=?", (gone_source,))
    assert table(mind, "mind_desire_archive") and table(mind, "mind_exploration_decision_archive")


def without_revision(text):
    state = json.loads(text)
    for key in ("revision", "updated_at"):
        state.pop(key)
    return dumps(state)


def test_the_migration_reads_applies_once_and_undoes_exactly(setup):
    mind, _, clock = setup
    legacy(mind, clock)
    revision, before = stored(mind)
    archives = {name: table(mind, name) for name in ("mind_desire_archive", "mind_exploration_decision_archive")}
    assert all(carrying(json.loads(data)) for rows in archives.values() for data in rows.values())

    dry = evidence_refs.run(mind)
    assert dry["state"] == "dry-run" and dry["marked"] is False
    assert dry["would_slim"] > 20 and dry["originals_kept"] == 3, dry
    assert dry["document"]["chars_after"] < dry["document"]["chars"] == len(before)
    for name, rows in archives.items():
        assert dry["archives"][name]["rows"] == len(rows) and dry["archives"][name]["changed_rows"] == len(rows)
        assert dry["archives"][name]["chars_after"] < dry["archives"][name]["chars"]
    assert stored(mind) == (revision, before), "a dry run writes nothing"
    assert {name: table(mind, name) for name in archives} == archives and originals(mind) == 0

    applied = evidence_refs.run(mind, apply=True)
    after_revision, after = stored(mind)
    assert applied["state"] == "slimmed" and applied["changed"] == dry["would_slim"] and applied["event_id"]
    assert after_revision == revision + 1 and kinds(mind)[-1] == evidence_refs.KIND
    # What the dry run said the document would be, apart from the revision and its time.
    assert abs(len(after) - dry["document"]["chars_after"]) <= 8
    state = json.loads(after)
    assert state[evidence_refs.MARK] == evidence_refs.SHAPE and not carrying(state)
    tombstones = [ref for _, ref in refs_in(state) if ref.get("erased")]
    assert tombstones and all("authority" in ref for ref in tombstones), "a tombstone is left as erasure wrote it"
    for name, before_rows in archives.items():
        rows = table(mind, name)
        assert not any(carrying(json.loads(data)) for data in rows.values())
        assert len(dumps(rows)) < len(dumps(before_rows))
    assert originals(mind) == 3

    again = evidence_refs.run(mind, apply=True)
    assert again["changed"] == 0 and again["event_id"] is None and stored(mind) == (after_revision, after)

    undo_dry = evidence_refs.run(mind, undo=True)
    assert undo_dry["state"] == "dry-run" and undo_dry["would_expand"] == applied["changed"]
    assert undo_dry["unexpandable"] == 0 and stored(mind) == (after_revision, after)

    undone = evidence_refs.run(mind, apply=True, undo=True)
    final_revision, final = stored(mind)
    assert undone["state"] == "expanded" and final_revision == after_revision + 1
    assert kinds(mind)[-1] == evidence_refs.UNDO_KIND
    assert without_revision(final) == without_revision(before), "byte for byte, apart from the revision and its time"
    assert {name: table(mind, name) for name in archives} == archives
    assert originals(mind) == 0

    assert evidence_refs.run(mind, apply=True, undo=True)["event_id"] is None
    assert stored(mind) == (final_revision, final)


def test_an_undo_after_new_writes_rebuilds_them_from_their_sources(setup):
    mind, _, clock = setup
    evidence_refs.run(mind, apply=True)
    wish = create(mind, clock, "later", [rich(mind, clock, "later")])
    evidence_refs.run(mind, apply=True, undo=True)
    state = json.loads(stored(mind)[1])
    ref = state["desires"][wish]["evidence"][0]
    with mind.engine.db.connect() as conn:
        assert ref == mind._reference(conn, ref["source_id"], ref["record_id"])
    assert evidence_refs.MARK not in state
    # Unmarked again: the next reference is written full, as before.
    other = create(mind, clock, "unmarked", [rich(mind, clock, "unmarked")])
    assert "metadata" in json.loads(stored(mind)[1])["desires"][other]["evidence"][0]


def test_the_operator_command_is_a_dry_run_unless_told_and_undoes_with_undo(setup, monkeypatch, capsys, tmp_path):
    import sys

    from kin_mind import host
    mind, _, clock = setup
    create(mind, clock, "cli", [rich(mind, clock, "cli")])
    config, credentials = tmp_path / "mind-config.json", tmp_path / "credentials"
    credentials.write_text("# no key: nothing here calls a model\n")
    config.write_text(json.dumps({"root": str(mind.engine.db.root), "scope": mind.scope.model_dump(),
                                  "agent_version": AGENT, "session_id": "synthetic-session",
                                  "credentials_file": str(credentials)}))

    def run(*flags):
        monkeypatch.setattr(sys, "argv", ["kin_mind.host", "--config", str(config), "slim-evidence-refs", *flags])
        with open("/dev/null") as empty:
            monkeypatch.setattr(sys, "stdin", empty)
            host.main()
        return json.loads(capsys.readouterr().out)
    before = stored(mind)
    assert run()["state"] == "dry-run" and stored(mind) == before
    assert run("--dry-run")["state"] == "dry-run" and stored(mind) == before
    assert run("--apply")["state"] == "slimmed" and not carrying(json.loads(stored(mind)[1]))
    assert run("--undo")["state"] == "dry-run"
    assert run("--undo", "--apply")["state"] == "expanded"
    assert without_revision(stored(mind)[1]) == without_revision(before[1])


def test_what_an_undo_needs_follows_a_wish_and_a_decision_into_their_archives_and_back(setup):
    """A reference an undo cannot rebuild from its source alone -- here one written before
    `received_at` was kept -- is remembered by the wish or decision it belongs to, not by where that
    sits: slimmed in the document, moved to its archive, undone there and put back, it is what it was."""
    mind, _, clock = setup
    MemoryContinuity(mind).configure({"desire_archive": True, "exploration_decision_archive": True})
    old = create(mind, clock, "old", [rich(mind, clock, "old")])
    mind.manage_desire(DesireChange(command_id="old-done", agent_version=AGENT, expected_revision=mind.read()["revision"],
                                    evidence_ids=[rich(mind, clock, "old-done")], action="complete", desire_id=old,
                                    reason="said"))
    for index in range(14):
        decided(mind, clock, f"explore_old{index:02d}")
    with mind.engine.db.connect(write=True) as conn:
        state = mind._load(conn)
        for entry in (state["desires"][old], state["exploration_decisions"]["explore_old00"]):
            for ref in entry["evidence"]:
                ref.pop("received_at")
        mind._save(conn, state)
    before = json.loads(stored(mind)[1])
    assert evidence_refs.run(mind, apply=True)["originals_kept"] >= 2
    clock[0] += timedelta(days=10)
    create(mind, clock, "new", [rich(mind, clock, "new")])
    desire_archive.archive(mind, apply=True, keep=1)
    decisions.archive(mind, apply=True)
    assert old in table(mind, "mind_desire_archive") and "explore_old00" in table(mind, "mind_exploration_decision_archive")
    evidence_refs.run(mind, apply=True, undo=True)
    assert json.loads(table(mind, "mind_desire_archive")[old]) == before["desires"][old]
    assert json.loads(table(mind, "mind_exploration_decision_archive")["explore_old00"]) == \
        before["exploration_decisions"]["explore_old00"]
    desire_archive.restore(mind, apply=True)
    decisions.restore(mind, apply=True)
    after = json.loads(stored(mind)[1])
    assert after["desires"][old] == before["desires"][old]
    assert after["exploration_decisions"]["explore_old00"] == before["exploration_decisions"]["explore_old00"]


def test_a_full_reference_a_release_before_wrote_into_a_marked_document_is_archived_slim(setup):
    """Rolled back without the undo and forward again, a marked document may hold references the
    release in between wrote whole. The next save keeps them to the trace, and a wish or a decision
    that moves in that same revision moves slim."""
    mind, _, clock = setup
    MemoryContinuity(mind).configure({"desire_archive": True, "exploration_decision_archive": True})
    evidence_refs.run(mind, apply=True)
    old = create(mind, clock, "old", [rich(mind, clock, "old")])
    mind.manage_desire(DesireChange(command_id="old-done", agent_version=AGENT, expected_revision=mind.read()["revision"],
                                    evidence_ids=[rich(mind, clock, "old-done")], action="complete", desire_id=old,
                                    reason="said"))
    for index in range(14):
        decided(mind, clock, f"explore_old{index:02d}")
    with mind.engine.db.connect(write=True) as conn:
        state = mind._load(conn)
        for entry in (state["desires"][old], *state["exploration_decisions"].values()):
            entry["evidence"] = [mind._reference(conn, ref["source_id"], ref["record_id"]) for ref in entry["evidence"]]
        # As the release in between wrote it: straight into the row, past this release's save.
        conn.execute("UPDATE mind_state SET data=? WHERE scope=?", (dumps(state), mind.scope.key()))
    assert carrying(json.loads(stored(mind)[1]))
    clock[0] += timedelta(days=10)
    create(mind, clock, "new", [rich(mind, clock, "new")])
    assert not carrying(json.loads(stored(mind)[1]))
    def written_whole(section, ids):
        with mind.engine.db.connect(write=True) as conn:
            state = mind._load(conn)
            for identifier in ids or list(state[section]):
                entry = state[section][identifier]
                entry["evidence"] = [mind._reference(conn, ref["source_id"], ref["record_id"]) for ref in entry["evidence"]]
            conn.execute("UPDATE mind_state SET data=? WHERE scope=?", (dumps(state), mind.scope.key()))
    written_whole("desires", [old])
    assert desire_archive.archive(mind, apply=True, keep=1)["moved_count"] == 1
    written_whole("exploration_decisions", None)
    assert decisions.archive(mind, apply=True)["moved_count"] >= 1
    for name in ("mind_desire_archive", "mind_exploration_decision_archive"):
        rows = table(mind, name)
        assert rows and not any(carrying(json.loads(data)) for data in rows.values()), name


def test_an_apply_that_finds_nothing_left_inside_its_transaction_writes_nothing(setup, monkeypatch):
    """Decided again inside the write: another apply may have slimmed everything between this one's
    survey and its transaction."""
    mind, _, clock = setup
    create(mind, clock, "raced", [rich(mind, clock, "raced")])
    evidence_refs.run(mind, apply=True)
    revision = stored(mind)
    real, calls = evidence_refs._plan, []

    def stale(conn, scope, state, *, undo):
        plan = real(conn, scope, state, undo=undo)
        calls.append(plan["counts"]["changed"])
        if len(calls) == 1:
            plan["counts"]["changed"] = 1  # the survey outside saw one left
        return plan
    monkeypatch.setattr(evidence_refs, "_plan", stale)
    answer = evidence_refs.run(mind, apply=True)
    assert len(calls) >= 2 and answer["event_id"] is None and answer["changed"] == 0
    assert stored(mind) == revision
