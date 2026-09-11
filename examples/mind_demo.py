"""Synthetic, temporary demonstration: mixed states and a durable contact receipt."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from eventmem.core import Engine
from eventmem.core.models import Scope, SourceInput
from kin_mind.state import AffectiveEvent, DesireChange, Mind

with TemporaryDirectory() as directory:
    engine = Engine(Path(directory))
    scope = Scope(persona="synthetic-demo")
    source = engine.receive(
        SourceInput(
            namespace="synthetic",
            key="role",
            text="A synthetic role configuration",
            scope=scope,
        )
    )
    mind = Mind(engine, scope)
    mind.initialize(agent_version="synthetic-demo-v1", evidence_ids=[source["id"]])
    experience = engine.receive(
        SourceInput(
            namespace="synthetic",
            key="finding",
            text="A completed experiment is worth sharing.",
            scope=scope,
            authority="operation",
        )
    )
    mind.record(
        AffectiveEvent(
            command_id="finding",
            agent_version="synthetic-demo-v1",
            expected_revision=1,
            evidence_ids=[experience["id"]],
            values={
                "longing": 85,
                "mood": 30,
                "focus": 92,
                "flirtation": 75,
                "initiative": 80,
            },
            reason="Synthetic mixed state after an experiment",
        )
    )
    mind.manage_desire(
        DesireChange(
            command_id="share",
            agent_version="synthetic-demo-v1",
            expected_revision=2,
            evidence_ids=[experience["id"]],
            action="create",
            content="Share the experiment result",
            topic="synthetic experiment",
            kind="contact",
            strength=90,
            expires_at=(datetime.now(timezone.utc) + timedelta(hours=8)).isoformat(),
            completion="The platform accepts the report",
            reason="A concrete result",
        )
    )
    before = mind.contact_candidate()
    attempt = mind.claim_contact(owner_epoch="synthetic-owner-message-1")
    mind.settle_contact(attempt_id=attempt["id"], state="pending")
    mind.settle_contact(
        attempt_id=attempt["id"], state="accepted", message_id="synthetic-receipt-only"
    )
    print(
        json.dumps(
            {
                "candidate_before": before["eligible"],
                "initiative_after": mind.read()["dimensions"]["initiative"]["value"],
                "wish_after": mind.read()["desires"][0]["status"],
                "delivery": mind.read()["desires"][0]["delivery"],
            },
            indent=2,
        )
    )
