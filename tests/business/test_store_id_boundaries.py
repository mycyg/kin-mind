"""A store id is found wherever the store names one, by the one pattern the store has (`db.NAMED`: ASCII
word boundaries, as the owned ACP reads a fork's tool results), so an id written right against Chinese
words is an id everywhere: in the receipts the repair's reerase clears, in what the maintenance step
keeps because the state cites it, in the unreadable archived row a history restore refuses, in the
approval the persona contract names and in what the evidence isolation counts. A value is read string
by string, never in its JSON, whose escapes would stand right before an id as a letter or a digit.
Each path is checked with the eleven boundaries the ACP's sync test pins (CL8-FLOW-03)."""
import json
import re
import sqlite3
from pathlib import Path

import pytest

from eventmem.core import Engine, read_policy, repair
from eventmem.core.db import NAMED, digest, dumps, named_in, named_in_json

from kin_mind import history_compaction
from kin_mind.deploy_checks import persona_approval
from kin_mind.isolation_migration import IsolationMigration

from test_deploy_checks import SCOPE, canon
from test_erasure import rounds, system  # noqa: F401  (the fixture)
from test_fork_reads import STORE_ID_BOUNDARIES
from test_repair import store  # noqa: F401  (the fixture)

NAMED_CASES = [text for text, named in STORE_ID_BOUNDARIES if named]


def test_the_store_id_pattern_is_written_once():
    """No module keeps a copy of the pattern: the erase, the repair, the read policy, the deploy checks
    and the archive restore take the store's own."""
    src = Path(__file__).resolve().parents[2] / "src"
    written = sorted(str(path.relative_to(src)) for path in src.rglob("*.py")
                     if "(?:src|mem)_[0-9a-f]{32}" in path.read_text(encoding="utf-8"))
    assert written == ["eventmem/core/db.py"]
    assert NAMED.flags & re.ASCII and repair.NAMED is history_compaction.NAMED is NAMED


def test_a_value_is_read_string_by_string_and_never_in_its_escaped_json():
    """Every one of the eleven ways a text can hold an id reads the same in a value, in the JSON a table
    keeps (Chinese as it is) and in JSON that escapes every Chinese character. In JSON a line break
    right before an id is the letter `n`, and hides it; read as the value, it does not."""
    identifier = "src_" + "ab" * 16
    for text, named in STORE_ID_BOUNDARIES:
        value = {"note": text.format(id=identifier), "other": ["无关"]}
        want = [identifier] if named else []
        assert named_in(value) == want, text
        assert named_in_json(dumps(value)) == want and named_in_json(json.dumps(value)) == want, text
    line = {"text": "第一行\n" + identifier}
    assert NAMED.findall(dumps(line)) == [], "in its JSON the id stands right after a letter"
    assert named_in(line) == named_in_json(dumps(line)) == [identifier]
    assert named_in({identifier: 1}) == [identifier] and named_in_json("不是 JSON：" + identifier) == [identifier]


def test_reerase_clears_a_receipt_that_names_a_deleted_id_right_against_chinese_words(store):
    """The delete clears every stored receipt that names what it deletes; a row written after it that
    names the id again -- as one a release before this one left -- is reerase's, and reerase runs once.
    Of the eleven rows, the six that name the id are counted by the dry run and cleared by the apply;
    the five where it is part of another word stay (CL8-FLOW-03)."""
    engine, mind, notes, owner, versions = store
    engine.delete(owner)
    with engine.db.connect(write=True) as conn:
        for index, (text, _) in enumerate(STORE_ID_BOUNDARIES):
            conn.execute("INSERT INTO metrics(name,value,created_at,data) VALUES(?,?,?,?)",
                         (f"boundary-{index}", 1, mind.clock(), dumps({"note": text.format(id=owner)})))
    root = engine.db.root
    assert repair.run(root, apply=False, steps=("reerase",))["steps"]["reerase"]["plan"]["receipts"] == len(NAMED_CASES)
    assert repair.run(root, apply=True, steps=("reerase",))["steps"]["reerase"]["done"]["receipts"] == len(NAMED_CASES)
    with engine.db.connect() as conn:
        left = {row[0] for row in conn.execute("SELECT name FROM metrics WHERE name LIKE 'boundary-%'")}
    assert left == {f"boundary-{index}" for index, (_, named) in enumerate(STORE_ID_BOUNDARIES) if not named}


@pytest.mark.parametrize("text,named", STORE_ID_BOUNDARIES)
def test_a_maintenance_note_the_state_names_right_against_chinese_words_stays_active(store, text, named):
    """The maintenance step archives a session-maintenance note only while neither the current state nor
    an appraisal still to run names it: named in the state's words, beside Chinese, it is kept."""
    engine, mind, notes, owner, versions = store
    with engine.db.connect(write=True) as conn:
        conn.execute("INSERT OR REPLACE INTO mind_state VALUES(?,?,?)",
                     (mind.scope.key(), 1, dumps({"session_advice": {"reason": text.format(id=notes[0])}})))
    plan = repair.run(engine.db.root, apply=False, steps=("origins", "maintenance"))["steps"]["maintenance"]["plan"]
    assert plan["kept_because_cited"] == (1 if named else 0)


def test_a_restore_refuses_an_unreadable_archived_row_that_names_erased_material_right_against_chinese_words(system, monkeypatch):
    """Nothing can be taken out of an archived row that will not parse, so a restore refuses one that
    names what was deleted since -- beside Chinese words too; one that names nothing deleted goes on."""
    mind, memory, source, clock = system
    secret = source("secret", "不该回来的话")
    rounds(mind, clock, source, 4)
    monkeypatch.setattr(history_compaction, "_quiet_check",
                        lambda mind, config: {"check": "quiescence", "ready": True, "reason": "store-is-quiet"})
    archive = history_compaction.archive_dir(mind.engine.db) / history_compaction.compact(mind, {}, apply=True)["archive"]
    mind.engine.delete(secret)
    with sqlite3.connect(archive) as conn:
        last = conn.execute("SELECT MAX(revision) FROM mind_events_v1").fetchone()[0]
    for text, named in STORE_ID_BOUNDARIES:
        broken = '{"note": "' + text.format(id=secret)
        with sqlite3.connect(archive) as conn:
            conn.execute("UPDATE mind_events_v1 SET data=?,data_sha256=? WHERE revision=?", (broken, digest(broken.encode()), last))
        if named:
            with pytest.raises(Exception) as refused:
                history_compaction.restore(mind, {}, apply=False)
            assert getattr(refused.value, "target", None) == "archive-row-unreadable-and-names-erased-material", text
        else:
            assert history_compaction.restore(mind, {}, apply=False)["state"] == "dry-run", text


def test_the_approval_the_persona_contract_names_is_found_value_by_value(tmp_path):
    """The read policy and the deploy checks read the approved source in the contract's values, not in
    JSON that escapes every Chinese character, where no pattern could find an id written against one."""
    for index, (text, named) in enumerate(STORE_ID_BOUNDARIES):
        config = canon(tmp_path / f"case-{index}")
        path = Path(config["root"]) / "persona-policy.json"
        policy = json.loads(path.read_text())
        approved = policy["approved_source"]
        path.write_text(json.dumps({**policy, "approved_source": text.format(id=approved)}))
        answer = persona_approval(config)
        assert (answer["ok"], answer["problems"]) == ((True, []) if named else (False, ["approved-source-missing"])), text
        engine = Engine(Path(config["root"]))
        read_policy._persona_cache.clear()
        with engine.db.connect() as conn:
            assert read_policy.approved_sources(engine, SCOPE, conn) == (frozenset({approved}) if named else frozenset()), text


def test_the_evidence_isolation_counts_a_cached_context_that_names_a_hidden_id_right_against_chinese_words(system):
    """The isolation's own pattern finds any identifier, a graph node's too, and ends words where the
    store's does: a cached context that names a hidden record or node beside Chinese words is counted."""
    mind, memory, source, clock = system
    hidden = {"mem_" + "ab" * 16, "node_" + "cd" * 12}
    with mind.engine.db.connect(write=True) as conn:
        for index, (text, _) in enumerate(STORE_ID_BOUNDARIES):
            for number, identifier in enumerate(sorted(hidden)):
                conn.execute("INSERT INTO mind_context_cache VALUES(?,?,?,?)",
                             (f"boundary-{index}-{number}", mind.scope.key(), dumps({"text": text.format(id=identifier)}), mind.clock()))
    with mind.engine.db.connect() as conn:
        assert IsolationMigration(mind)._caches(conn, hidden)["context_cache_rows"] == len(hidden) * len(NAMED_CASES)
