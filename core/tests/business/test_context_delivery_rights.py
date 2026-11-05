"""Who may append a prepared context, and how every unsettled one can be read (CR-LIFE-09).

Only the call that moves a delivery from prepared to sending takes the send right; a record that
was already sending is handed back as it is. The pending list can be read page by page in id
order, so a caller is never limited to the oldest sixteen."""
import pytest

from eventmem.core import Engine
from eventmem.core.models import Scope, SourceInput

from kin_mind.context import Contexts
from kin_mind.context_delivery import ContextDelivery
from kin_mind.state import Mind

SCOPE = Scope(persona="synthetic-delivery-rights")


@pytest.fixture
def delivery(tmp_path):
    engine = Engine(tmp_path / "db")
    mind = Mind(engine, SCOPE)
    first = engine.receive(SourceInput(namespace="kin-owner-input", key="init", text="setup", scope=SCOPE,
                                       authority="explicit", extract=False, occurred_at="2026-09-01T00:00:00+00:00",
                                       metadata={"role": "user", "host_event": "message"}))["id"]
    mind.initialize(agent_version="fixture-v1", evidence_ids=[first])
    return ContextDelivery(Contexts(mind))


def test_only_the_call_that_takes_the_send_right_may_append(delivery):
    session = "thread-rights"
    prepared = delivery.prepare(session, "initial", "event-1", "synthetic context", [])
    taken = delivery.begin(session, "initial", prepared["id"])
    assert taken["state"] == "sending" and taken["acquired"] is True
    again = delivery.begin(session, "initial", prepared["id"])
    assert again["state"] == "sending" and "acquired" not in again
    # The flag belongs to the answer, never to the stored record.
    assert "acquired" not in delivery.pending(session)[0]


def test_every_unsettled_delivery_can_be_read_page_by_page(delivery):
    session = "thread-pages"
    ids = []
    for n in range(20):
        prepared = delivery.prepare(session, "initial", f"event-{n}", f"synthetic context {n}", [])
        delivery.begin(session, "initial", prepared["id"])
        ids.append(prepared["id"])
    assert len(delivery.pending(session)) == 16  # the oldest first, as before
    seen, after = [], ""
    while True:
        page = delivery.pending(session, after=after, limit=16)
        seen += [row["id"] for row in page]
        if len(page) < 16:
            break
        after = page[-1]["id"]
    assert seen == sorted(ids)


def test_known_unadmitted_native_context_reacquires_only_before_acceptance(delivery):
    session = "native-canceled"
    prepared = delivery.prepare(session, "initial", "event-canceled", "synthetic context", [])
    identity = prepared["id"]
    delivery.begin(session, "initial", identity)
    delivery.uncertain(session, "initial", identity)
    assert "acquired" not in delivery.begin(session, "initial", identity)
    retry = delivery.begin(session, "initial", identity, not_admitted=True)
    assert retry["acquired"] is True and retry["text"] == prepared["text"]
    delivery.acknowledge(session, "initial", identity, actual_session=session,
                         marker=prepared["marker"], verified=True, native_item_id="synthetic-native-message")
    assert "acquired" not in delivery.begin(session, "initial", identity, not_admitted=True)


def test_unadmitted_reacquisition_rechecks_window_and_erasure(delivery):
    session = "native-revalidate"
    prepared = delivery.prepare(session, "initial", "event-window", "synthetic context", [])
    delivery.begin(session, "initial", prepared["id"])
    with delivery.db.connect(write=True) as conn:
        window = delivery.ctx.window(session, conn)
        window["epoch"] = "next-window"
        delivery.ctx._save_window(conn, session, window)
    stale = delivery.begin(session, "initial", prepared["id"], not_admitted=True)
    assert stale["state"] == "stale" and "acquired" not in stale
    fresh = delivery.prepare(session, "next-window", "event-erased", "synthetic context", [])
    delivery.begin(session, "next-window", fresh["id"])
    from kin_mind.erasure import erased_delivery
    with delivery.db.connect(write=True) as conn:
        value = delivery._get(conn, session, "next-window", fresh["id"])
        delivery._put(conn, erased_delivery(value, delivery.ctx.mind.clock()))
    erased = delivery.begin(session, "next-window", fresh["id"], not_admitted=True)
    assert erased["state"] == "erased" and "acquired" not in erased
