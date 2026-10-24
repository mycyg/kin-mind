"""A failed contact attempt is the host's, never Kin's decision (2026-10-01).

On 2026-09-28, 14 of the 17 canceled attempts were drafts that failed, and each carried the host's
fallback wait as its `decision` -- just like the three that were Kin's own. Since then a wait the host
puts the wishes in is kept as `host_wait` (with `decided_by: "host"` and its `cause`) and the wish's
`contact_wait` says so; `decision` holds hers alone. The backoff is as it was. Older rows are not
rewritten: they are read by the host's own words and reasons. Counts of how attempts ended
(`outcomes_24h`), the state Kin is shown and her initiative facts all hold her choices apart.

The resident worker answers the liveness facts the host's contact pause and fault watch read
(`liveness-facts`)."""
import io
import json
from datetime import timedelta

from kin_mind import initiative
from kin_mind.host import RESIDENT_ACTIONS, dispatch, serve
from kin_mind.interaction_projection import interaction_projection
from kin_mind.operational_status import contact_outcome, liveness_read
from kin_mind.state import CONTACT_WAIT_MIN_SECONDS, HOST_WAIT_REASONS, host_wait, timestamp

from test_kin_mind import setup, wish  # noqa: F401  (the store fixture)

LIMIT = {"category": "model-output", "stage": "mind-worker", "code": "mind-worker-output-limit",
         "retry_condition": "backoff", "model_invoked": None}


def row(mind, attempt_id):
    with mind.engine.db.connect() as conn:
        found = conn.execute("SELECT state,data FROM mind_contacts WHERE id=?", (attempt_id,)).fetchone()
    return found["state"], json.loads(found["data"])


def raw_wish(mind, identifier):
    with mind.engine.db.connect() as conn:
        return mind._load(conn)["desires"][identifier]


def shown_wish(mind, identifier):
    return next(d for d in mind.read()["desires"] if d["id"] == identifier)


def outcomes(mind):
    return liveness_read(mind, "contact", now=timestamp(mind.clock()).timestamp())["contact_failures"]["outcomes_24h"]


def test_native_draft_reason_survives_contact_settlement(setup):
    mind, source, clock = setup
    wish(mind, source, "rain", content="Tell her about the rain")
    attempt = mind.claim_contact(owner_epoch="owner-1")
    failure = {"category": "model-unavailable", "stage": "contact-draft-model",
               "code": "fork-draft-failed", "retry_condition": "backoff", "model_invoked": False,
               "native_error": "no-completed-turn", "native_reason": "native-history-unavailable",
               "native_stage": "not-started"}
    mind.settle_contact(attempt_id=attempt["id"], state="canceled", reason="draft-not-started", failure=failure)
    _, data = row(mind, attempt["id"])
    for key, value in failure.items():
        assert data["failure"][key] == value


def test_a_failed_draft_records_technical_failure_without_changing_kins_wish(setup):
    mind, source, clock = setup
    wish(mind, source, "rain", content="Tell her about the rain")
    attempt = mind.claim_contact(owner_epoch="owner-1")
    did = attempt["desire_ids"][0]
    before = raw_wish(mind, did)
    mind.settle_contact(attempt_id=attempt["id"], state="canceled", reason="draft-failed", failure=LIMIT)
    state, data = row(mind, attempt["id"])
    assert state == "canceled" and data["failure"]["code"] == "mind-worker-output-limit"
    assert not {"decision", "decisions", "host_wait", "host_waits"} & data.keys()
    assert raw_wish(mind, did) == before, "technical retry state belongs to the attempt, not a semantic wish"
    assert outcomes(mind) == {"sent": 0, "unconfirmed": 0, "kin_wait": 0, "kin_abandon": 0, "host": 1}
    clock[0] += timedelta(hours=25)
    unsent = initiative.facts(mind)["contact_wishes_unsent_a_day"]
    assert [(entry["desire_id"], entry["status"]) for entry in unsent] == [(did, "wanted")]


def test_her_own_wait_stays_her_decision(setup):
    mind, source, clock = setup
    wish(mind, source, "trip", content="Ask about her trip")
    attempt = mind.claim_contact(owner_epoch="owner-1")
    did = attempt["desire_ids"][0]
    mine = {"action": "wait", "condition": "owner_reply", "reason": "She is travelling; ask when she is back"}
    mind.settle_contact(attempt_id=attempt["id"], state="canceled", reason="draft-decision", decision=mine)
    _, data = row(mind, attempt["id"])
    assert data["decision"]["reason"] == mine["reason"] and data["decisions"][did]["action"] == "wait"
    assert "host_wait" not in data and "host_waits" not in data
    assert "decided_by" not in raw_wish(mind, did)["contact_wait"]
    assert "decided_by" not in shown_wish(mind, did)["contact_wait"]
    assert outcomes(mind)["kin_wait"] == 1 and outcomes(mind)["host"] == 0
    clock[0] += timedelta(hours=25)
    assert "wait_decided_by" not in initiative.facts(mind)["contact_wishes_unsent_a_day"][0]


def test_an_older_row_is_read_by_its_words_and_its_reason_and_left_as_it_is(setup):
    """A row a release before wrote: the host's fallback wait as its `decision`, the wish's wait without
    `decided_by`. Nothing rewrites it; what reads it knows it for the host's all the same."""
    mind, source, _clock = setup
    wish(mind, source, "older", content="An older wish")
    attempt = mind.claim_contact(owner_epoch="owner-1")
    did = attempt["desire_ids"][0]
    mind.settle_contact(attempt_id=attempt["id"], state="canceled", reason="draft-failed", failure=LIMIT)
    with mind.engine.db.connect(write=True) as conn:
        state, data = conn.execute("SELECT state,data FROM mind_contacts WHERE id=?", (attempt["id"],)).fetchone()
        data = json.loads(data)
        # Load the old persisted shape explicitly; the current writer no longer creates it.
        old = {"action": "wait", "reason": "Draft generation or parsing failed", "condition": "time", "retry_after_seconds": CONTACT_WAIT_MIN_SECONDS}
        data.update(decision=old, decisions={did: old})
        conn.execute("UPDATE mind_contacts SET data=? WHERE id=?", (json.dumps(data), attempt["id"]))
        current = mind._load(conn)
        current["desires"][did]["status"] = "waiting"
        current["desires"][did]["contact_wait"] = {**old, "since": mind.clock(), "retry_at": (timestamp(mind.clock()) + timedelta(seconds=CONTACT_WAIT_MIN_SECONDS)).isoformat()}
        mind._save(conn, current)
    assert "decided_by" not in raw_wish(mind, did)["contact_wait"]
    assert shown_wish(mind, did)["contact_wait"]["decided_by"] == "host", "known by the host's words"
    assert "decided_by" not in raw_wish(mind, did)["contact_wait"], "and the row is not rewritten"
    assert outcomes(mind)["host"] == 1 and outcomes(mind)["kin_wait"] == 0, "known by its reason"
    # The classifier, for every way an attempt ends.
    assert contact_outcome("canceled", "draft-failed", "wait") == "host"
    assert contact_outcome("canceled", "Host action failed", None) == "host"
    assert contact_outcome("canceled", "Delivery conditions changed before sending", None) == "host"
    assert contact_outcome("canceled", "draft-decision", "wait") == "kin_wait"
    assert contact_outcome("canceled", "draft-decision", "abandon") == "kin_abandon"
    assert contact_outcome("canceled", "draft-decision", "wait", "host") == "host"
    assert contact_outcome("accepted", None, None) == "sent"
    assert contact_outcome("unconfirmed", "receipt-still-unknown", None) == "unconfirmed"
    assert contact_outcome("drafting", None, None) is None
    # The test the readers use, for an old wait and a new one.
    assert host_wait({"reason": "The draft could not start yet"}) and not host_wait({"reason": "她在旅行"})
    assert not host_wait({"reason": "Draft generation or parsing failed", "decided_by": "kin"}) and not host_wait(None)


def test_every_technical_cancel_leaves_the_semantic_wish_unchanged(setup):
    mind, source, _clock = setup
    for reason in ("draft-failed", "draft-not-started", "contact-review-failed", "draft-source-changed",
                   "contact-source-changed", "repeats-unconfirmed-send", "draft-sources-deleted", "draft-empty"):
        wish(mind, source, "wish-" + reason, content="A wish for " + reason)
        attempt = mind.claim_contact(owner_epoch="owner-1")
        before = {identifier: raw_wish(mind, identifier) for identifier in attempt["desire_ids"]}
        mind.settle_contact(attempt_id=attempt["id"], state="canceled", reason=reason)
        for identifier, value in before.items():
            assert raw_wish(mind, identifier) == value
        _, data = row(mind, attempt["id"])
        assert "decision" not in data and "host_wait" not in data
    assert outcomes(mind)["host"] == 8


def test_the_resident_worker_answers_the_liveness_facts(setup):
    mind, source, _clock = setup
    config = {"root": str(mind.engine.db.root), "scope": mind.scope.model_dump(), "agent_version": "synthetic-v1",
              "session_id": "synthetic-session"}
    for key in ("one", "two", "three"):
        wish(mind, source, key, content="Wish " + key)
        attempt = mind.claim_contact(owner_epoch="owner-1")
        mind.settle_contact(attempt_id=attempt["id"], state="canceled", reason="draft-failed", failure=LIMIT)
    assert "liveness-facts" in RESIDENT_ACTIONS
    contact = dispatch(config, "liveness-facts", {"part": "contact"})
    assert set(contact) == {"checked_at", "contact_failures"}
    streak = contact["contact_failures"]["streak"]
    assert (streak["count"], streak["code"]) == (3, "mind-worker-output-limit")
    whole = dispatch(config, "liveness-facts", {})
    assert whole["contact_failures"]["streak"]["count"] == 3 and "last" in whole
    assert "service" not in (whole.get("embeddings") or {}), "the embedding service is not asked"
    out = io.StringIO()
    serve(config, io.StringIO(json.dumps({"id": "a", "action": "liveness-facts", "args": {"part": "contact"}}) + "\n"
                              + json.dumps({"id": "b", "action": "liveness-facts", "args": {"part": "words"}}) + "\n"), out)
    answers = [json.loads(line) for line in out.getvalue().splitlines()]
    assert answers[0]["ok"] and answers[0]["result"]["contact_failures"]["streak"]["count"] == 3
    assert not answers[1]["ok"], "an unknown part is refused"
