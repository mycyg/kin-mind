"""Shared dialogue stays shared without assigning another person's words to the current user."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from kin_mind.dialogue import dialogue_rows, recent_dialogue, redundant_public_summaries, split_recent
from kin_mind.memory import MemoryContinuity
from kin_mind.model_view import for_model
from kin_mind.session_checkpoint import SessionCheckpoint

pytest_plugins = ("test_kin_mind",)


@pytest.fixture(autouse=True)
def fixed_identity_clock(setup):
    # Synthetic dialogue and the observer share a deterministic time basis.
    setup[2][0] = datetime(2030, 4, 8, 10, 0, tzinfo=timezone.utc)


def utterance(number, person="xiaoguang", kind="owner-message", **extra):
    route = "chat:" + person
    return {"id": "identity-test-" + str(number), "kind": kind,
            "at": (datetime(2030, 4, 8, 9, 0, tzinfo=timezone.utc) + timedelta(seconds=number)).isoformat(),
            "text": "请把三张植物卡片按颜色排好", "channel": "feishu",
            "speaker_person_id": "person:" + person if kind == "owner-message" else "agent:kin",
            "recipient_person_id": "person:" + person, "conversation_key": route,
            "conversation_kind": "p2p", "chat_id": route, "actor": "person:" + person if kind == "owner-message" else "agent:kin",
            "audience_person_ids": ["person:" + person],
            **({"state": "accepted", "message_id": "message-" + str(number)} if kind == "delivery" else {}), **extra}


def record(mind, events):
    memory = MemoryContinuity(mind)
    memory.configure({"records": True})
    for event in events:
        memory.ingest(event)
    return memory


def assert_identity(items, events):
    actual = {item["id"]: item for item in items}
    assert set(actual) == {event["id"] for event in events}
    for event in events:
        for field in ("speaker_person_id", "recipient_person_id", "conversation_kind", "actor", "audience_person_ids"):
            assert actual[event["id"]].get(field) == event[field], (event["id"], field)
        assert "conversation_key" not in actual[event["id"]] and "chat_id" not in actual[event["id"]]
        assert actual[event["id"]]["role"] == ("user" if event["kind"] == "owner-message" else "assistant")


def test_shared_semantic_context_and_worker_keep_both_people_and_group_speaker(setup):
    from kin_mind.decision_context import execution_brief
    mind, _, _ = setup
    events = [utterance(1), utterance(2, kind="delivery"), utterance(3, "yiyi"),
              utterance(4, "yiyi", conversation_key="chat:family", chat_id="chat:family",
                        conversation_kind="group", recipient_person_id="group:family",
                        audience_person_ids=["person:xiaoguang", "person:yiyi"])]
    memory = record(mind, events)
    assert_identity(recent_dialogue(mind), events)
    assert_identity(for_model(memory.semantic_context())["recent_interaction"], events)
    assert_identity(for_model(execution_brief(mind, question="continue", evidence_ids=[]))["recent_dialogue"], events)


@pytest.mark.parametrize("first_kind,second_kind", [("delivery", "assistant-message"), ("assistant-message", "delivery")])
@pytest.mark.parametrize("view", ["recent", "checkpoint"])
def test_same_text_in_different_conversations_survives_recent_and_checkpoint(setup, first_kind, second_kind, view):
    mind, _, _ = setup
    events = [utterance(1, kind=first_kind), utterance(2, "yiyi", kind=second_kind)]
    record(mind, events)
    if view == "recent":
        assert_identity(recent_dialogue(mind), events)
        return
    checkpoint = SessionCheckpoint(mind)
    snapshot = checkpoint.snapshot()
    assert_identity(snapshot["items"], events)
    built = checkpoint.build(snapshot, {"conversationId": "shared", "generation": 1}, allow_model=False)
    assert built["complete"]
    assert_identity(checkpoint.payload(built)["publicHistory"], events)


def test_same_conversation_receipt_and_aggregate_still_deduplicate(setup):
    mind, _, _ = setup
    delivery, aggregate = utterance(1, kind="delivery"), utterance(2, kind="assistant-message")
    record(mind, [delivery, aggregate])
    assert [item["id"] for item in recent_dialogue(mind)] == [delivery["id"]]
    assert [item["id"] for item in SessionCheckpoint(mind).snapshot()["items"]] == [delivery["id"]]


def test_legacy_unknown_identity_is_not_filled_from_adjacent_person(setup):
    mind, _, _ = setup
    legacy = {"id": "legacy-output", "kind": "assistant-message", "text": "请把三张植物卡片按颜色排好",
              "at": utterance(2)["at"]}
    known = utterance(1, kind="delivery")
    record(mind, [known, legacy])
    for items in (recent_dialogue(mind), SessionCheckpoint(mind).snapshot()["items"]):
        assert {item["id"] for item in items} == {known["id"], legacy["id"]}
        unknown = next(item for item in items if item["id"] == legacy["id"])
        assert not unknown.get("recipient_person_id") and not unknown.get("conversation_key")


def test_aggregate_does_not_join_bubbles_from_different_conversations():
    one, two = utterance(1, kind="delivery", text="one"), utterance(2, "yiyi", kind="delivery", text="two")
    aggregate = utterance(3, kind="assistant-message", text="one\n\ntwo")
    assert redundant_public_summaries([one, two, aggregate]) == set()
    same_route = {**two, **{key: one[key] for key in ("recipient_person_id", "conversation_key", "chat_id", "audience_person_ids")}}
    assert redundant_public_summaries([one, same_route, aggregate]) == {aggregate["id"]}


@pytest.mark.parametrize("route_change", [False, True])
def test_window_counts_speaker_and_conversation_changes_as_new_inputs(route_change):
    first = {**utterance(1), "role": "user"}
    second = {**utterance(2), "role": "user"}
    if route_change:
        second["conversation_key"] = "chat:other"
    else:
        second["speaker_person_id"] = "person:yiyi"
    assert split_recent([first, second], exchanges=1) == ([second], [first])
    same_speaker = {**first, "id": "next-bubble"}
    assert split_recent([first, same_speaker], exchanges=1) == ([first, same_speaker], [])


def test_window_does_not_attach_another_conversations_preceding_answer():
    items = [{**utterance(0), "role": "user"},
             {**utterance(1, kind="delivery"), "role": "assistant"},
             {**utterance(2, "yiyi"), "role": "user"}]
    assert split_recent(items, exchanges=1) == (items[2:], items[:2])


def test_pagination_uses_same_identity_boundaries_as_window(setup):
    import json
    mind, _, _ = setup
    record(mind, [utterance(0)])
    with mind.engine.db.connect() as conn:
        for number in range(1, 130):
            event = utterance(number, "xiaoguang" if number % 2 else "yiyi")
            conn.execute("INSERT INTO mind_runtime_events(scope,id,kind,occurred_at,digest,data) VALUES(?,?,?,?,?,?)",
                         (mind.scope.key(), event["id"], event["kind"], event["at"], event["id"], json.dumps(event)))
    assert len(dialogue_rows(mind, exchanges=4)) == 128, "the first page already has many separate input blocks"


def test_revalidation_preserves_identity(setup):
    from kin_mind.revalidation import _request
    mind, _, _ = setup
    events = [utterance(1), utterance(2, "yiyi")]
    record(mind, events)
    recent = recent_dialogue(mind)
    stored = SimpleNamespace(receipt={}, conflict={}, proposal={})
    request = _request(SimpleNamespace(mind=mind), {}, {"recent_dialogue": recent}, stored, [])
    for expected, shown in zip(events, request["recent_dialogue"]):
        for field in ("speaker_person_id", "recipient_person_id", "actor", "audience_person_ids"):
            assert shown.get(field) == expected[field]
        assert "conversation_key" not in shown and "chat_id" not in shown


def test_model_source_projection_preserves_recipient_actor_and_audience():
    identity = {key: value for key, value in utterance(1, kind="delivery").items()
                if key in {"speaker_person_id", "recipient_person_id", "conversation_key", "conversation_kind", "chat_id", "actor", "audience_person_ids"}}
    shown = for_model({"id": "src_" + "a" * 32, "text": "same words", "metadata": {**identity, "index_only": "private-index"}})
    assert "metadata" not in shown and "index_only" not in shown
    routes = {"conversation_key", "chat_id"}
    assert all(shown.get(field) == value for field, value in identity.items() if field not in routes)
    assert not routes & set(shown)
