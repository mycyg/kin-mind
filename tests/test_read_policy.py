"""Purpose-typed recall: role configuration, synthetic examples, self-knowledge claims and host
envelopes are never recalled as shared experience, whatever `history` says, and are never shown
as `explicit`. An audit read still returns all of them, labelled. An owner turn is never among
them: what the owner said about a configuration is recalled, labelled. Synthetic data only."""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from eventmem.core import Engine
from eventmem.core.api import create_app
from eventmem.core.jobs import Worker
from eventmem.core.mcp import create_mcp
from eventmem.core.models import (
    RecallQuery,
    RecallRequest,
    RecordInput,
    RevisionInput,
    Scope,
    SourceInput,
    now,
)
from eventmem.core.providers import Providers
from eventmem.core.read_policy import (
    CLASSES,
    MIGRATION,
    RULES_VERSION,
    ReadPolicy,
    classify_scope,
    configure_registry,
    record_classes,
    registry,
    source_rule,
)
from eventmem.core.reading import read_segment
from eventmem.core.self_knowledge import ClaimInput, SelfKnowledge

SCOPE = Scope(persona="synthetic-reader")
ENVELOPE = "内部探索选题事件，宿主要求挑选一个 lantern 选题，不发送消息。"
# Specimens that must never be recalled as experience, with the class each is labelled with.
SPECIMENS = {"claim": "self_knowledge", "example": "synthetic_example", "envelope": "host_envelope",
             "configuration": "role_configuration"}


def root(engine, source):
    return next(rid for rid in engine.source(source["id"])["record_ids"] if not engine.get(rid)["evidence_ids"])


def receive(engine, namespace, key, text, *, scope=SCOPE, authority="explicit", **metadata):
    return root(engine, engine.receive(SourceInput(namespace=namespace, key=key, text=text, scope=scope,
                                                   authority=authority, metadata=metadata)))


def make_legacy(engine):
    """What the store looked like before this policy existed: no class rows and no insert stamps.
    Test scaffolding only; the code under test never rewrites a stored record."""
    with engine.db.connect(write=True) as conn:
        conn.execute("DELETE FROM source_evidence_class")
        conn.execute("UPDATE records SET data=json_remove(data,'$.attributes.origin_kind')")
        conn.execute("UPDATE revisions SET data=json_remove(data,'$.attributes.origin_kind')")
        engine.db.bump(conn)


def stored(engine):
    with engine.db.connect() as conn:
        return ([tuple(r) for r in conn.execute("SELECT id,revision,data FROM records ORDER BY id")],
                [tuple(r) for r in conn.execute("SELECT record_id,revision,data FROM revisions ORDER BY record_id,revision")])


@pytest.fixture
def world(tmp_path):
    """One experience, one owner configuration request and the four kinds of non-experience,
    all answering the same query. `legacy` strips what insert-time classification wrote."""
    engine = Engine(tmp_path / "memory")
    configure_registry(engine, {"synthetic-persona-store": "role_configuration"})
    ids = {"lived": receive(engine, "chat", "lived", "We walked through the lantern market together.",
                            role="user", host_event="message")}
    ids["request"] = receive(engine, "synthetic-persona-store", "request", "Please speak warmly about lantern evenings.",
                             role="user", host_event="configuration-request")
    ids["configuration"] = receive(engine, "synthetic-persona-store", "installed",
                                   "Installed role text: the agent speaks warmly about lantern evenings.",
                                   authority="operation", role="host", host_event="configuration-verified")
    ids["example"] = receive(engine, "chat", "example", "Example dialogue: we watched lantern boats last winter.",
                             role="assistant", host_event="message", examples_are_synthetic=True)
    ids["envelope"] = receive(engine, "chat", "envelope", ENVELOPE, role="user", host_event="message")
    claim = SelfKnowledge(engine, SCOPE).claim(ClaimInput(
        command_id="role-claim", aspect="voice", context="chat", agent_version="config-1", basis="role",
        claim="I describe lantern evenings warmly.", evidence_ids=[ids["request"]]))
    ids["claim"] = claim["id"]
    return engine, ids


def recall(engine, query="lantern", **options):
    return engine.recall(RecallRequest(scope=SCOPE, query=query, explain=True, limit=50, **options))


def assert_isolated(engine, ids, channel, *, only=SPECIMENS, **options):
    """(a) absent from experience recall with and without history, (b) never `explicit`,
    (c) present and labelled with its class under audit; claims also in the self-knowledge view."""
    for history in (False, True):
        result = recall(engine, history=history, **options)
        returned = {i["id"] for i in result["items"]}
        found = {rid for rid in result["trace"]["channels"].get(channel, [])}
        for name in only:
            assert ids[name] in found, (channel, name, "the channel must reach the record for the test to mean anything")
            assert ids[name] not in returned and ids[name] not in result["text"], (channel, name, history)
        assert {f["reason"] for f in result["trace"]["filtered"] if f["id"] in {ids[n] for n in only}} <= {
            "self_knowledge_requires_versioned_view", *("not_experience:" + kind for kind in CLASSES)}
    audit = recall(engine, history=True, recall_purpose="audit", **options)
    items = {i["id"]: i for i in audit["items"]}
    for name in only:
        item = items[ids[name]]
        assert item["evidence_class"] == SPECIMENS[name] and item["confirmation"] == SPECIMENS[name]
        line = next(line for line in audit["text"].splitlines() if line.startswith("[" + ids[name]))
        assert "[" + SPECIMENS[name] in line and "explicit" not in line.split("] ", 2)[1]
    if "claim" in only:
        view = recall(engine, history=True, recall_purpose="self_knowledge_view", **options)
        assert {i["id"]: i for i in view["items"]}[ids["claim"]]["evidence_class"] == "self_knowledge"
        assert "[self_knowledge role config-1]" in view["text"]
        assert ids["envelope"] not in {i["id"] for i in view["items"]}
    return audit


@pytest.mark.parametrize("legacy", [False, True])
def test_fts_fast_mode_never_returns_non_experience(world, legacy):
    engine, ids = world
    if legacy:
        make_legacy(engine)
    audit = assert_isolated(engine, ids, "fts", mode="fast")
    # An audit answer is never served to an experience read from the recall cache, and a read
    # that accounts for a session judges by the same policy inside its write transaction.
    for options in ({}, {"session": "synthetic-session"}):
        lived = recall(engine, mode="fast", **options)
        assert ids["lived"] in {i["id"] for i in lived["items"]}
        assert {ids[name] for name in SPECIMENS}.isdisjoint(i["id"] for i in lived["items"])
    assert {i["id"]: i for i in audit["items"]}[ids["lived"]]["confirmation"] == "explicit"
    assert "evidence_class" not in {i["id"]: i for i in audit["items"]}[ids["lived"]]


def test_fts_deep_mode_never_returns_non_experience(world):
    engine, ids = world
    make_legacy(engine)
    assert_isolated(engine, ids, "fts", mode="deep")


def test_historical_known_at_path_never_returns_non_experience(world):
    engine, ids = world
    make_legacy(engine)
    assert_isolated(engine, ids, "historical", known_at=now())


@pytest.mark.parametrize("name", sorted(SPECIMENS))
def test_exact_id_never_returns_non_experience(world, name):
    engine, ids = world
    make_legacy(engine)
    assert_isolated(engine, ids, "exact", only=[name], query=ids[name])


def test_recent_channel_never_returns_non_experience(world):
    engine, ids = world
    make_legacy(engine)
    assert_isolated(engine, ids, "recent", query="")


def test_prefetch_channel_never_returns_non_experience(world):
    engine, ids = world
    make_legacy(engine)
    with engine.db.connect(write=True) as conn:
        for name in SPECIMENS:
            conn.execute("INSERT INTO prefetch VALUES(?,?,?,?)", (SCOPE.key(), "zeppelin", ids[name], 1))
        engine.db.bump(conn)
    result = recall(engine, query="zeppelin")
    assert not result["trace"]["channels"].get("fts")
    assert_isolated(engine, ids, "prefetch", query="zeppelin")


def test_vector_channel_never_returns_non_experience(world):
    pytest.importorskip("lancedb")
    from eventmem.core.vectors import VectorIndex

    engine, ids = world
    make_legacy(engine)
    index_id = VectorIndex.register(engine, "synthetic-vector-model", 4, "test-v1")
    VectorIndex(engine, index_id).upsert([
        {"id": ids[name], "scope": SCOPE.key(), "revision": 1, "vector": [1.0, 0.0, 0.0, float(n) / 10]}
        for n, name in enumerate(["lived", *SPECIMENS])])
    options = {"query": "unmatchedterm", "vector": [1.0, 0.0, 0.0, 0.0], "index": index_id}
    assert_isolated(engine, ids, "vector", **options)
    assert ids["lived"] in {i["id"] for i in recall(engine, **options)["items"]}


def test_relation_seeds_exclude_what_the_policy_refuses(world):
    engine, ids = world
    make_legacy(engine)
    neighbour = receive(engine, "chat", "neighbour", "An unrelated note about harbour tides.", role="user", host_event="message")
    engine.relate(ids["claim"], "supports", neighbour)
    for history in (False, True):
        result = recall(engine, history=history)
        assert ids["claim"] in result["trace"]["channels"]["fts"]
        # A refused claim no longer pulls what it is related to into the ranking.
        assert neighbour not in result["trace"]["channels"].get("graph", [])
        assert neighbour not in {i["id"] for i in result["items"]}
    audit = recall(engine, history=True, recall_purpose="audit")
    assert neighbour in audit["trace"]["channels"]["graph"] and neighbour in {i["id"] for i in audit["items"]}
    # Only the policy unseats a seed. A superseded hit still leads to the record that replaced it.
    old = receive(engine, "chat", "old", "The lantern stall opens at six.", role="user", host_event="message")
    current = receive(engine, "chat", "current", "The stall now opens at seven.", role="user", host_event="message")
    engine.revise(old, RevisionInput(expected_revision=1, command_id="replace-old", action="replace", replacement_id=current))
    result = recall(engine, mode="deep")  # the full lexical ranking, which also finds inactive records
    assert {"id": old, "reason": "status:superseded"} in result["trace"]["filtered"]
    assert current in result["trace"]["channels"]["graph"] and current in {i["id"] for i in result["items"]}


def test_owner_configuration_requests_stay_experience_with_a_label(world):
    engine, ids = world
    for legacy in (False, True):
        if legacy:
            make_legacy(engine)
        result = recall(engine)
        item = {i["id"]: i for i in result["items"]}[ids["request"]]
        assert item["confirmation"] == "explicit" and item["evidence_label"] == "configuration_request"
        assert "evidence_class" not in item
        line = next(line for line in result["text"].splitlines() if line.startswith("[" + ids["request"]))
        assert "] [configuration_request] Please speak warmly" in line
        policy = ReadPolicy.load(engine, SCOPE)
        assert policy.visible(engine.get(ids["request"])) and policy.label(engine.get(ids["request"])) == "configuration_request"
        assert policy.basis(engine.get(ids["request"])) == "explicit"
        # The host's own text in the same namespace is configuration, not a request.
        assert ids["configuration"] not in {i["id"] for i in result["items"]}


FLAGS = ({"examples_are_synthetic": True}, {"example_kind": "dialogue"}, {"example_count": 3},
         {"configuration_only": True})


@pytest.mark.parametrize("flag", FLAGS)
def test_a_flag_labels_the_owners_own_turn_and_hides_everyone_elses(tmp_path, flag):
    """Saying that the examples above are made up is itself something the owner said. The same
    flag on text the owner did not write, and on what a model derived from it, still hides it."""
    engine = Engine(tmp_path / "memory")
    asked = receive(engine, "chat", "asked", "Treat the lantern dialogues above as samples.", role="user", **flag)
    copied = receive(engine, "chat", "copied", "Three sample lantern dialogues follow.", role="assistant", **flag)
    guessed = receive(engine, "chat", "guessed", "A lantern sample the model wrote down.", authority="model",
                      role="user", **flag)
    derived = engine.add_record(RecordInput(
        kind="fact", content="The lantern dialogues above are samples.", scope=SCOPE, generated=True,
        source_ids=engine.get(asked)["source_ids"], attributes=dict(flag)), "derived")["id"]
    hidden, kind = (copied, guessed, derived), "role_configuration" if "configuration_only" in flag else "synthetic_example"
    for legacy in (False, True):
        if legacy:
            make_legacy(engine)
        record = engine.get(asked)
        policy = ReadPolicy.load(engine, SCOPE)
        assert policy.classify(record) == ("experience", "configuration_request", "owner-configuration-request")
        assert policy.basis(record) == record["confirmation"] == "explicit"
        assert policy.prefix(record) == "[configuration_request] "
        for history in (False, True):
            result = recall(engine, history=history)
            returned = {i["id"]: i for i in result["items"]}
            assert returned[asked]["confirmation"] == "explicit" and "evidence_class" not in returned[asked]
            assert returned[asked]["evidence_label"] == "configuration_request"
            assert "] [configuration_request] Treat the lantern" in next(
                line for line in result["text"].splitlines() if line.startswith("[" + asked))
            assert set(hidden).isdisjoint(returned), (legacy, history)
        assert read_segment(engine, asked)["evidence_label"] == "configuration_request"
        assert {i["id"]: i for i in engine.list_records(SCOPE)["items"]}[asked]["evidence_label"] == "configuration_request"
        # Reachable all along: an audit read returns every one of them, with its class.
        audit = {i["id"]: i for i in recall(engine, history=True, recall_purpose="audit")["items"]}
        assert all(audit[rid]["evidence_class"] == audit[rid]["confirmation"] == kind for rid in hidden)


def test_an_envelope_and_a_self_claim_are_read_before_who_wrote_them(tmp_path):
    """Two rules come before authorship: a host prompt stored as an owner turn, which only its
    text tells apart, and a self-knowledge entry, whose evidence is the owner's own words."""
    engine = Engine(tmp_path / "memory")
    prompt = receive(engine, "chat", "prompt", ENVELOPE, role="user", host_event="message",
                     examples_are_synthetic=True)
    lived = receive(engine, "chat", "lived", "We walked through the lantern market together.",
                    role="user", host_event="message")
    claim = SelfKnowledge(engine, SCOPE).claim(ClaimInput(
        command_id="role-claim", aspect="voice", context="chat", agent_version="config-1", basis="role",
        claim="I describe lantern evenings warmly.", evidence_ids=[lived]))["id"]
    with engine.db.connect(write=True) as conn:
        conn.execute("UPDATE records SET data=json_set(data,'$.attributes.role','user') WHERE id=?", (claim,))
        engine.db.bump(conn)
    policy = ReadPolicy.load(engine, SCOPE)
    assert policy.classify(engine.get(prompt)) == ("host_envelope", None, "host-envelope-content")
    assert policy.classify(engine.get(claim)) == ("self_knowledge", None, "self-knowledge-entry")
    returned = {i["id"] for i in recall(engine, history=True)["items"]}
    assert lived in returned and {prompt, claim}.isdisjoint(returned)


def test_only_an_operator_row_hides_an_owner_turn(tmp_path):
    """A row is where a person's review of a source is recorded, and its rule name says whether
    a person wrote it. What the automatic classification stores never hides what the owner said."""
    engine = Engine(tmp_path / "memory")
    configure_registry(engine, {"private-role-store": "role_configuration"})
    asked = receive(engine, "private-role-store", "asked", "Please keep the lantern wording.", role="user")
    source = engine.get(asked)["source_ids"][0]
    with engine.db.connect(write=True) as conn:
        record_classes(engine, conn, SCOPE, [{"source_id": source, "class": "role_configuration",
                                              "rule": "namespace-registry"}])
    assert ReadPolicy.load(engine, SCOPE).classify(engine.get(asked)) == (
        "experience", "configuration_request", "owner-configuration-request")
    assert asked in {i["id"] for i in recall(engine, history=True)["items"]}
    with engine.db.connect(write=True) as conn:
        record_classes(engine, conn, SCOPE, [{"source_id": source, "class": "role_configuration",
                                              "rule": "operator-review"}])
    assert ReadPolicy.load(engine, SCOPE).classify(engine.get(asked)) == ("role_configuration", None, "operator-review")
    assert asked not in {i["id"] for i in recall(engine, history=True)["items"]}
    audit = {i["id"]: i for i in recall(engine, history=True, recall_purpose="audit")["items"]}
    assert audit[asked]["evidence_class"] == "role_configuration"


def test_a_flagged_owner_source_is_proposed_and_stamped_as_a_request(tmp_path):
    engine = Engine(tmp_path / "memory")
    asked = receive(engine, "chat", "asked", "Treat the lantern dialogues above as samples.",
                    role="user", examples_are_synthetic=True)
    record = engine.get(asked)
    source = record["source_ids"][0]
    with engine.db.connect() as conn:
        row = conn.execute("SELECT class,rule FROM source_evidence_class WHERE source_id=?", (source,)).fetchone()
        proposed = {p["source_id"]: p for p in classify_scope(engine, conn, SCOPE)}
    # The insert stamp is the label; a class the owner's own words never earn is not stamped.
    assert record["attributes"]["origin_kind"] == "configuration_request"
    assert tuple(row) == (proposed[source]["class"], proposed[source]["rule"]) == ("experience", "owner-configuration-request")


def test_new_sources_are_stamped_at_insert_without_a_revision_bump(world):
    engine, ids = world
    with engine.db.connect() as conn:
        rows = {r["source_id"]: dict(r) for r in conn.execute("SELECT * FROM source_evidence_class")}
        revisions = dict(conn.execute("SELECT record_id,COUNT(*) FROM revisions GROUP BY record_id").fetchall())
    expected = {"request": ("experience", "owner-configuration-request", "configuration_request"),
                "configuration": ("role_configuration", "namespace-registry", "role_configuration"),
                "example": ("synthetic_example", "metadata-synthetic-example", "synthetic_example"),
                "envelope": ("host_envelope", "host-envelope-content", "host_envelope")}
    for name, (kind, rule, stamp) in expected.items():
        record = engine.get(ids[name])
        row = rows[record["source_ids"][0]]
        assert (row["class"], row["rule"], row["rules_version"], row["scope"]) == (kind, rule, RULES_VERSION, SCOPE.key())
        assert record["attributes"]["origin_kind"] == stamp
        assert record["revision"] == 1 and revisions[ids[name]] == 1
    lived = engine.get(ids["lived"])
    assert "origin_kind" not in lived["attributes"] and lived["source_ids"][0] not in rows
    # The source keeps what the caller sent; the stamp is the engine's and cannot be self-awarded.
    forged = receive(engine, "chat", "forged", "A model note about lantern prices.", authority="model",
                     role="assistant", origin_kind="configuration_request")
    assert "origin_kind" not in engine.get(forged)["attributes"]
    assert engine.source(engine.get(forged)["source_ids"][0])["metadata"]["origin_kind"] == "configuration_request"
    declared = receive(engine, "chat", "declared", "Host-declared role text about lantern manners.",
                       authority="operation", role="host", origin_kind="role_configuration")
    assert engine.get(declared)["attributes"]["origin_kind"] == "role_configuration"
    assert declared not in {i["id"] for i in recall(engine, history=True)["items"]}
    # Receiving the same source again is the same receipt: nothing is stamped or classified twice.
    before = stored(engine)
    engine.receive(SourceInput(namespace="synthetic-persona-store", key="installed", scope=SCOPE, authority="operation",
                               text="Installed role text: the agent speaks warmly about lantern evenings.",
                               metadata={"role": "host", "host_event": "configuration-verified"}))
    assert stored(engine) == before
    # An erased source takes its class row with it.
    erased = engine.get(ids["configuration"])["source_ids"][0]
    engine.delete(erased)
    with engine.db.connect() as conn:
        assert not conn.execute("SELECT 1 FROM source_evidence_class WHERE source_id=?", (erased,)).fetchone()


def test_order_of_authority_between_rows_flags_envelopes_and_namespace_rules(tmp_path):
    engine = Engine(tmp_path / "memory")
    counted = receive(engine, "chat", "counted", "Three sample lantern dialogues follow.", role="user", example_count=3)
    copied = receive(engine, "chat", "copied", "Three more sample lantern dialogues follow.", role="assistant", example_count=3)
    prompt = receive(engine, "private-role-store", "prompt", ENVELOPE, role="user", host_event="message")
    make_legacy(engine)
    configure_registry(engine, {"private-role-store": "role_configuration"})
    policy = ReadPolicy.load(engine, SCOPE)
    # A flag says what a text is about. It never outranks the owner having said it.
    assert policy.classify(engine.get(counted)) == ("experience", "configuration_request", "owner-configuration-request")
    assert policy.classify(engine.get(copied)) == ("synthetic_example", None, "metadata-synthetic-example")
    # The namespace rule alone would call an owner-role turn a request. The text says envelope.
    assert policy.classify(engine.get(prompt)) == ("host_envelope", None, "host-envelope-content")
    with engine.db.connect() as conn:
        proposed = {p["source_id"]: p for p in classify_scope(engine, conn, SCOPE)}
    assert proposed[engine.get(prompt)["source_ids"][0]]["class"] == "host_envelope"
    assert proposed[engine.get(copied)["source_ids"][0]]["class"] == "synthetic_example"
    asked = proposed[engine.get(counted)["source_ids"][0]]
    assert (asked["class"], asked["rule"]) == ("experience", "owner-configuration-request")
    # A stored row has the last word: an operator's review can clear what a flag would hide.
    with engine.db.connect(write=True) as conn:
        record_classes(engine, conn, SCOPE, [{"source_id": engine.get(copied)["source_ids"][0], "class": "experience", "rule": "operator-review"}])
    assert ReadPolicy.load(engine, SCOPE).classify(engine.get(copied)) == ("experience", None, "operator-review")
    assert {counted, copied} <= {i["id"] for i in recall(engine)["items"]} and prompt not in {i["id"] for i in recall(engine, history=True)["items"]}


def test_insert_stamp_still_classifies_when_rows_and_rules_are_gone(tmp_path):
    engine = Engine(tmp_path / "memory")
    configure_registry(engine, {"private-role-store": "role_configuration"})
    dump = receive(engine, "private-role-store", "dump", "Verified role text about the lantern.", authority="operation", role="host")
    ask = receive(engine, "private-role-store", "ask", "Please keep the lantern wording.", role="user")
    configure_registry(engine, {})
    with engine.db.connect(write=True) as conn:
        conn.execute("DELETE FROM source_evidence_class")
        engine.db.bump(conn)
    policy = ReadPolicy.load(engine, SCOPE)
    assert policy.classify(engine.get(dump)) == ("role_configuration", None, "insert-stamp")
    # The stamp is one more configuration signal on an owner turn, and only labels it.
    assert policy.classify(engine.get(ask)) == ("experience", "configuration_request", "owner-configuration-request")
    returned = {i["id"]: i for i in recall(engine, history=True)["items"]}
    assert dump not in returned and returned[ask]["evidence_label"] == "configuration_request"


def test_legacy_records_are_classified_without_being_mutated(tmp_path):
    engine = Engine(tmp_path / "memory")
    lived = receive(engine, "chat", "lived", "We repaired the lantern frame.", role="user", host_event="message")
    dump = receive(engine, "private-role-store", "dump", "Verified role text mentioning the lantern frame.",
                   authority="operation", role="host")
    request = receive(engine, "private-role-store", "ask", "Please remember the lantern frame wording.", role="user")
    derived = engine.add_record(RecordInput(
        kind="fact", content="The role text mentions a lantern frame.", scope=SCOPE, generated=True,
        source_ids=engine.get(dump)["source_ids"], evidence_ids=[dump]), "derived")["id"]
    mixed = engine.add_record(RecordInput(
        kind="fact", content="The lantern frame was repaired and is part of the role text.", scope=SCOPE, generated=True,
        confirmation="verified", source_ids=[*engine.get(dump)["source_ids"], *engine.get(lived)["source_ids"]]), "mixed")["id"]
    # Nothing marks these yet: the namespace is unknown and legacy data carries no row or stamp.
    assert dump in {i["id"] for i in recall(engine, history=True)["items"]}
    before = stored(engine)
    configure_registry(engine, {"private-role-store": "role_configuration"})
    with engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_evidence_class").fetchone()[0] == 0
    for history in (False, True):
        returned = {i["id"] for i in recall(engine, history=history)["items"]}
        assert {dump, derived}.isdisjoint(returned)
        # A record with real experience behind it stays, and the owner's request stays, labelled.
        assert {lived, request, mixed} <= returned
    audit = {i["id"]: i for i in recall(engine, history=True, recall_purpose="audit")["items"]}
    assert audit[dump]["evidence_class"] == audit[derived]["evidence_class"] == "role_configuration"
    assert audit[request]["evidence_label"] == "configuration_request" and "evidence_class" not in audit[mixed]
    engine.list_records(SCOPE), read_segment(engine, dump), engine.source(engine.get(dump)["source_ids"][0])
    assert stored(engine) == before


def test_half_migrated_store_hides_what_a_migrated_one_hides(tmp_path):
    engine = Engine(tmp_path / "memory")
    first = receive(engine, "private-role-store", "one", "Role text one about the lantern.", authority="operation", role="host")
    second = receive(engine, "private-role-store", "two", "Role text two about the lantern.", authority="operation", role="host")
    configure_registry(engine, {"private-role-store": "role_configuration"})
    with engine.db.connect() as conn:
        proposed = classify_scope(engine, conn, SCOPE)
    assert {p["source_id"] for p in proposed} == {engine.get(r)["source_ids"][0] for r in (first, second)}
    assert {(p["class"], p["rule"], p["namespace"]) for p in proposed} == {("role_configuration", "namespace-registry", "private-role-store")}
    # The migration stops after its first row. Both sources still read the same way.
    with engine.db.connect(write=True) as conn:
        assert record_classes(engine, conn, SCOPE, proposed[:1]) == 1
    assert {first, second}.isdisjoint({i["id"] for i in recall(engine, history=True)["items"]})
    with engine.db.connect(write=True) as conn:
        record_classes(engine, conn, SCOPE, proposed)
        record_classes(engine, conn, SCOPE, proposed)
        assert conn.execute("SELECT COUNT(*) FROM source_evidence_class").fetchone()[0] == 2
    # A row outranks the rules: the migration may clear a source the registry would hide.
    with engine.db.connect(write=True) as conn:
        record_classes(engine, conn, SCOPE, [{**proposed[0], "class": "experience", "rule": "operator-review"}])
    cleared = next(r for r in (first, second) if engine.get(r)["source_ids"][0] == proposed[0]["source_id"])
    assert cleared in {i["id"] for i in recall(engine)["items"]}
    with pytest.raises(ValueError), engine.db.connect(write=True) as conn:
        record_classes(engine, conn, SCOPE, [{**proposed[0], "class": "invented"}])


def test_strict_flag_follows_the_migration_row(tmp_path):
    from kin_mind.memory import MemoryContinuity
    from kin_mind.state import Mind

    engine = Engine(tmp_path / "memory")
    assert ReadPolicy.load(engine, SCOPE).migration_state is None and not ReadPolicy.load(engine, SCOPE).strict
    MemoryContinuity(Mind(engine, SCOPE))
    for state, strict in (("classified", True), ("complete", False)):
        with engine.db.connect(write=True) as conn:
            conn.execute("INSERT OR REPLACE INTO mind_memory_migrations VALUES(?,?,?,?)",
                         (SCOPE.key(), MIGRATION, 0, json.dumps({"state": state, "rules_version": RULES_VERSION})))
        policy = ReadPolicy.load(engine, SCOPE)
        assert (policy.migration_state, policy.strict, policy.rules_version) == (state, strict, RULES_VERSION)


def test_persona_approved_sources_are_configuration(tmp_path):
    engine = Engine(tmp_path / "memory")
    text = receive(engine, "chat", "role-text", "Approved role wording about lantern manners.", authority="operation", role="host")
    ask = receive(engine, "chat", "approval", "I approve the lantern manners wording.", role="user", host_event="message")
    assert {text, ask} <= {i["id"] for i in recall(engine)["items"]}
    (engine.db.root / "persona-policy.json").write_text(json.dumps({
        "scope": SCOPE.model_dump(), "approved_source": [engine.get(text)["source_ids"][0], ask]}))
    returned = {i["id"]: i for i in recall(engine, history=True)["items"]}
    assert text not in returned and returned[ask]["evidence_label"] == "configuration_request"
    audit = {i["id"]: i for i in recall(engine, recall_purpose="audit")["items"]}
    assert audit[text]["evidence_class"] == "role_configuration"
    # Another scope's contract says nothing about this one.
    (engine.db.root / "persona-policy.json").write_text(json.dumps({
        "scope": Scope(persona="other").model_dump(), "approved_source": engine.get(text)["source_ids"]}))
    assert text in {i["id"] for i in recall(engine)["items"]}


def test_labels_on_read_memory_list_records_and_source(world):
    engine, ids = world
    make_legacy(engine)
    listed = {i["id"]: i for i in engine.list_records(SCOPE)["items"]}
    for name, kind in SPECIMENS.items():
        read = read_segment(engine, ids[name])
        assert read["confirmation"] == kind and read["evidence_class"] == kind
        assert listed[ids[name]]["confirmation"] == kind and listed[ids[name]]["evidence_class"] == kind
        assert listed[ids[name]]["attributes"] == {}
    assert engine.get(ids["claim"])["confirmation"] == "explicit"  # the stored record is untouched
    for name in ("lived", "request"):
        assert read_segment(engine, ids[name])["confirmation"] == listed[ids[name]]["confirmation"] == "explicit"
        assert "evidence_class" not in listed[ids[name]]
    assert listed[ids["request"]]["evidence_label"] == read_segment(engine, ids["request"])["evidence_label"] == "configuration_request"
    sources = {name: engine.source(engine.get(ids[name])["source_ids"][0]) for name in ("lived", "request", "configuration", "example", "envelope")}
    assert "evidence_class" not in sources["lived"] and "basis" not in sources["lived"]
    assert sources["request"]["evidence_label"] == "configuration_request" and "evidence_class" not in sources["request"]
    for name in ("configuration", "example", "envelope"):
        assert sources[name]["evidence_class"] == sources[name]["basis"] == SPECIMENS[name]


@pytest.mark.asyncio
async def test_mcp_recall_memory_has_no_purpose_and_history_stays_experience(world):
    engine, ids = world
    make_legacy(engine)
    server = create_mcp(engine)
    tools = {tool.name: tool for tool in await server.list_tools()}
    assert "recall_purpose" not in json.dumps(tools["recall_memory"].inputSchema)
    assert "recall_purpose" not in RecallQuery.model_fields and "recall_purpose" in RecallRequest.model_fields
    # No read tool of the chat model can say what a read is for.
    for name in ("recall_memory", "read_memory", "read_continuity_context", "source_evidence", "memory_history",
                 "read_event_thread", "read_graph", "browse_topics", "read_self_knowledge"):
        assert not any("purpose" in key for key in tools[name].inputSchema.get("properties", {})), name
    result = await server.call_tool("recall_memory", {"request": {"scope": SCOPE.model_dump(), "query": "lantern", "history": True, "limit": 50}})
    recalled = json.loads(result[0].text)
    returned = {i["id"] for i in recalled["items"]}
    assert {ids["lived"], ids["request"]} <= returned
    assert all(ids[name] not in returned and ids[name] not in recalled["text"] for name in SPECIMENS)
    with pytest.raises(Exception, match="recall_purpose"):
        await server.call_tool("recall_memory", {"request": {"scope": SCOPE.model_dump(), "query": "lantern", "recall_purpose": "audit"}})
    read = json.loads((await server.call_tool("read_memory", {"record_id": ids["claim"]}))[0].text)
    assert read["confirmation"] == read["evidence_class"] == "self_knowledge"


def test_wire_field_is_optional_and_typed(world):
    engine, ids = world
    assert "recall_purpose" not in RecallRequest(query="lantern").model_dump(exclude_defaults=True)
    with TestClient(create_app(engine=engine, token="test-credential", workers=False)) as client:
        headers = {"Authorization": "Bearer test-credential"}
        body = {"scope": SCOPE.model_dump(), "query": "lantern", "history": True, "limit": 50}
        default = client.post("/v1/recall", headers=headers, json=body).json()
        assert ids["claim"] not in {i["id"] for i in default["items"]}
        audit = client.post("/v1/recall", headers=headers, json=body | {"recall_purpose": "audit"}).json()
        assert {i["id"]: i for i in audit["items"]}[ids["claim"]]["confirmation"] == "self_knowledge"
        assert client.post("/v1/recall", headers=headers, json=body | {"recall_purpose": "everything"}).status_code == 422
        listed = client.get("/v1/memories", headers=headers, params={"persona": SCOPE.persona}).json()
        assert {i["id"]: i for i in listed["items"]}[ids["envelope"]]["confirmation"] == "host_envelope"
        read = client.get("/v1/memories/" + ids["configuration"], headers=headers).json()
        assert read["confirmation"] == read["evidence_class"] == "role_configuration"


def test_narrative_job_is_given_experience_only(world, monkeypatch):
    engine, ids = world
    make_legacy(engine)
    seen = []

    def narrate(self, role, instruction, payload, **kwargs):
        seen.append({r["id"] for r in payload["records"]})
        return {"content": "A lantern evening.", "evidence_ids": [ids["lived"]]}

    monkeypatch.setattr(Providers, "json", narrate)
    job = {"id": "job_synthetic", "kind": "diary", "payload": json.dumps({"scope": SCOPE.model_dump()})}
    Worker(engine).prepare(job)
    assert {ids["lived"], ids["request"]} <= seen[0]
    assert seen[0].isdisjoint({ids[name] for name in SPECIMENS})


def switch_off(engine, scope=SCOPE):
    from kin_mind.memory import MemoryContinuity
    from kin_mind.state import Mind

    MemoryContinuity(Mind(engine, scope)).configure({"recall_purpose_policy": False})


def test_switch_off_restores_the_previous_behaviour(world, monkeypatch):
    engine, ids = world
    neighbour = receive(engine, "chat", "neighbour", "An unrelated note about harbour tides.", role="user", host_event="message")
    engine.relate(ids["claim"], "supports", neighbour)
    switch_off(engine)
    assert not ReadPolicy.load(engine, SCOPE, "audit").enabled
    plain = recall(engine)
    returned = {i["id"]: i for i in plain["items"]}
    # Before: only self-knowledge was held back, and only without `history`.
    assert ids["claim"] not in returned and {ids["envelope"], ids["example"], ids["configuration"]} <= set(returned)
    assert {"id": ids["claim"], "reason": "self_knowledge_requires_versioned_view"} in plain["trace"]["filtered"]
    assert neighbour in plain["trace"]["channels"]["graph"]  # seeds were taken before validation
    assert all("evidence_class" not in i and "evidence_label" not in i for i in returned.values())
    historical = recall(engine, history=True)
    assert "[role explicit config-1]" in historical["text"]
    assert {i["id"]: i for i in historical["items"]}[ids["claim"]]["confirmation"] == "explicit"
    assert recall(engine, history=True, recall_purpose="audit")["text"] == historical["text"]
    listed = {i["id"]: i for i in engine.list_records(SCOPE)["items"]}
    assert listed[ids["claim"]]["confirmation"] == "explicit" and "evidence_class" not in listed[ids["claim"]]
    assert "evidence_class" not in read_segment(engine, ids["configuration"])
    assert "evidence_class" not in engine.source(engine.get(ids["configuration"])["source_ids"][0])
    seen = []
    monkeypatch.setattr(Providers, "json", lambda self, role, instruction, payload, **k: seen.append(
        {r["id"] for r in payload["records"]}) or {"content": "A lantern evening.", "evidence_ids": [ids["lived"]]})
    Worker(engine).prepare({"id": "job_synthetic", "kind": "diary", "payload": json.dumps({"scope": SCOPE.model_dump()})})
    assert {ids["claim"], ids["envelope"], ids["configuration"]} <= seen[0]


def test_source_rules_are_narrow_and_declarations_cannot_vouch():
    rules = (("store", "role_configuration"), ("examples:*", "synthetic_example"), ("envelopes", "host_envelope"))
    assert source_rule("chat", {"role": "user"}, "explicit", rules=rules) is None
    assert source_rule("chat", {"origin_kind": "experience"}, "model", rules=rules) is None
    assert source_rule("store", {"role": "host"}, "operation", rules=rules)[:2] == ("role_configuration", None)
    assert source_rule("store", {"role": "user"}, "explicit", rules=rules)[:2] == ("experience", "configuration_request")
    # Owner words are a request only when they are the owner's: a model or host source is not.
    assert source_rule("store", {"role": "user"}, "model", rules=rules).kind == "role_configuration"
    assert source_rule("examples:v2", {}, "document", rules=rules).kind == "synthetic_example"
    assert source_rule("envelopes", {"role": "user"}, "explicit", rules=rules).kind == "host_envelope"
    assert source_rule("store", {"role": "user"}, "explicit", rules=rules, text=ENVELOPE).kind == "host_envelope"
    # A flag on the owner's own turn labels it; the same flag on anyone else's text hides it.
    assert source_rule("chat", {"configuration_only": True, "role": "user"}, "explicit", rules=rules)[:2] == ("experience", "configuration_request")
    assert source_rule("chat", {"configuration_only": True, "role": "assistant"}, "explicit", rules=rules).kind == "role_configuration"
    assert source_rule("chat", {"example_kind": "dialogue"}, "explicit", rules=rules).kind == "synthetic_example"
    assert source_rule("chat", {}, "explicit", source_id="src_a", approved={"src_a"}, rules=rules).rule == "persona-approved-source"


def test_registry_is_public_rules_plus_host_additions(tmp_path):
    engine = Engine(tmp_path / "memory")
    assert dict(registry(engine))["role-configuration"] == "role_configuration"
    merged = dict(configure_registry(engine, {"private-store": "role_configuration", "private-examples*": "synthetic_example"}))
    assert merged["private-store"] == "role_configuration" and merged["role-configuration"] == "role_configuration"
    for bad in ({"private-store": "experience"}, {"": "role_configuration"}, {"*": "role_configuration"}, ["private-store"]):
        with pytest.raises(ValueError):
            configure_registry(engine, bad)
    with pytest.raises(ValueError):
        ReadPolicy.load(engine, SCOPE, "everything")


def test_node_visibility_follows_the_evidence_behind_a_node(world):
    engine, ids = world
    make_legacy(engine)
    policy, audit = ReadPolicy.load(engine, SCOPE), ReadPolicy.load(engine, SCOPE, "audit")

    def ref(name):
        record = engine.get(ids[name])
        source = engine.source(record["source_ids"][0])
        return {"source_id": source["id"], "record_id": record["id"], "namespace": source["namespace"],
                "metadata": source["metadata"], "authority": source["authority"]}

    configured = {"id": "node_configured", "kind": "entity", "evidence": [ref("configuration"), ref("example")]}
    mixed = {"id": "node_mixed", "kind": "entity", "evidence": [ref("configuration"), ref("lived")]}
    requested = {"id": "node_requested", "kind": "event", "evidence": [ref("request")]}
    assert not policy.node_visible(configured) and audit.node_visible(configured)
    assert policy.node_label(configured) == "synthetic_example"
    assert policy.node_visible(mixed) and policy.node_label(mixed) is None
    assert policy.node_visible(requested) and policy.node_label(requested) == "configuration_request"
    # The projection of a self-claim cites the owner's words, so only the record itself tells.
    claim = engine.get(ids["claim"])
    projected = {"id": claim["id"], "kind": "self_narrative", "text": "", "record_ids": [claim["id"]], "evidence": [ref("request")]}
    assert policy.node_visible(projected)
    assert not policy.node_visible(projected, {claim["id"]: claim})
    assert policy.node_label(projected, {claim["id"]: claim}) == "self_knowledge"
    assert ReadPolicy.load(engine, SCOPE, "self_knowledge_view").node_visible(projected, {claim["id"]: claim})
    edge = {"id": "edge_configured", "kind": "edge", "subject": "a", "object": "b", "evidence": [ref("envelope")]}
    assert policy.node_visible(edge)  # an envelope source has no namespace or metadata mark; its record has
    assert not policy.node_visible(edge, {ids["envelope"]: engine.get(ids["envelope"])})


def test_every_valid_call_passes_the_read_policy():
    """`valid(data, request, policy)`: no call site may decide alone what counts as experience."""
    calls = {}
    for path in sorted(Path(__file__).resolve().parents[1].joinpath("src").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None)) == "valid":
                assert len(node.args) >= 3 or any(k.arg == "policy" for k in node.keywords), (path.name, node.lineno)
                calls[path.name] = calls.get(path.name, 0) + 1
    # The eight sites of the design brief. A new one is welcome here once it passes the policy.
    assert calls == {"retrieval.py": 2, "context.py": 1, "adaptive_recall.py": 5}
    import inspect

    from eventmem.core.retrieval import valid
    assert inspect.signature(valid).parameters["policy"].default is inspect.Parameter.empty
