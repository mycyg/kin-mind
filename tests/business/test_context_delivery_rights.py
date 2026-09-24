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
