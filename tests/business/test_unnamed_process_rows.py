"""The rows of a model's or an executor's own process that do not name what it was shown besides
what they cite -- as every one a release before this one wrote -- lose their words to any delete: no
delete can be matched with what their model saw. Their ids, states and receipts stay; a queue row
still to be judged also loses what it kept to take up, and is judged afresh. The repair's reerase
counts them by table and takes them all, rows left at work included, since nothing runs then
(CL6D-MM-04)."""
import json

from eventmem.core import repair

from kin_mind import erasure
from kin_mind.appraisal import Appraisals
from kin_mind.erasure import ERASED, UNNAMED_MARK
from kin_mind.revalidation import candidate

pytest_plugins = ('test_kin_mind',)

OLD = "xyzzy-before-release"
NEW = "xyzzy-named"
RECEIPT = {"provider": "deepseek", "model": "synthetic", "native_turn_id": "turn-1",
           "tool_calls": [{"name": "memory_recall", "ok": True, "ids": []}]}
TABLES = {
    "mind_daily_reviews": "CREATE TABLE IF NOT EXISTS mind_daily_reviews(scope TEXT,day TEXT,state TEXT,data TEXT,PRIMARY KEY(scope,day))",
    "mind_explorations": "CREATE TABLE IF NOT EXISTS mind_explorations(id TEXT PRIMARY KEY,scope TEXT NOT NULL,state TEXT NOT NULL,"
                         "created_at TEXT NOT NULL,data TEXT NOT NULL)",
    "mind_plan_runs": "CREATE TABLE IF NOT EXISTS mind_plan_runs(id TEXT PRIMARY KEY,scope TEXT NOT NULL,plan_id TEXT NOT NULL,"
                      "step_id TEXT NOT NULL,actor TEXT NOT NULL,state TEXT NOT NULL,lease_until REAL NOT NULL,owner TEXT NOT NULL,"
                      "fence INTEGER NOT NULL,data TEXT NOT NULL)",
}


def process_rows(mind, source):
    """One of each: rows as the release before this one wrote them (`old-*`), rows still at work
    (`busy-*`), and rows of this release, which name what they were shown (`new-*`)."""
    Appraisals(mind)
    scope, at, seen = mind.scope.key(), mind.clock(), source("seen", "她说过的一件事")
    ref = {"source_id": seen, "record_id": "mem_" + "0" * 32, "hash": "h", "revision": 1}
    rows = {
        "mind_appraisals": {
            "old-complete": ("complete", {"evidence_ids": [seen], "agent_version": "old-v1", "evaluated_sources": [ref],
                                          "proposed_result": {"reason": f"想起 {OLD}", "values": {"curiosity": 60}},
                                          "receipt": RECEIPT, "result": {"event_id": "mind_old"}}),
            "old-pending": ("pending", {"evidence_ids": [seen], "agent_version": "old-v1", "proposed_result": {"reason": OLD},
                                        "error_detail": {"class": "conflict", "code": "revision-moved", "message": f"没有提交：{OLD}"},
                                        "reuse": {"proposal": {"reason": OLD}, "receipt": RECEIPT, "manifest_digest": "m", "conflict": {},
                                                  "n": 0, "sources": [ref]},
                                        "tier": "reuse", "frozen_memory_context": {"text": f"记忆：{OLD}", "tokens": 4}}),
            "old-seed": ("pending", {"evidence_ids": [seen], "stimulus": "memory-enrichment", "seed_sources": [ref],
                                     "seed_memory": {"summary": f"关于 {OLD}"}, "seed_receipt": RECEIPT}),
            "busy-running": ("running", {"evidence_ids": [seen], "proposed_result": {"reason": OLD}, "attempt_token": "t"}),
            "new-complete": ("complete", {"evidence_ids": [seen], "evaluated_ids": [seen], "proposed_result": {"reason": NEW},
                                          "receipt": RECEIPT}),
        },
        "mind_daily_reviews": {
            "2026-09-01": ("complete", {"receipt": RECEIPT, "reason": f"今天 {OLD}", "evidence": [ref]}),
            # The behaviour chain's own review asks no model: nothing it holds was written from a view.
            "2026-09-02": ("complete", {"proposal_id": "proposal-1", "compat": "c", "model_calls": 0,
                                        "result": {"event_id": "mind_chain", "reason": OLD}}),
            "2026-09-03": ("complete", {"receipt": RECEIPT, "reason": NEW, "evidence": [ref], "evaluated_ids": [seen]}),
        },
        "mind_contacts": {
            "old-contact": ("accepted", {"desire_ids": ["wish-1"], "reason": OLD, "text_excerpt": f"想和你说 {OLD}",
                                         "draft_receipt": RECEIPT, "updated_at": at}),
            "busy-contact": ("drafting", {"desire_ids": ["wish-2"], "reason": OLD}),
            "new-contact": ("accepted", {"desire_ids": ["wish-3"], "reason": NEW, "evaluated_ids": [seen], "updated_at": at}),
        },
        "mind_explorations": {
            "old-exploration": ("complete", {"desire_id": "wish-1", "evidence_ids": [seen], "selected_brief": OLD,
                                             "result": {"summary": f"查到 {OLD}", "findings": [OLD]}, "source_id": "src_" + "1" * 32}),
            "busy-exploration": ("running", {"desire_id": "wish-2", "evidence_ids": [seen], "selected_brief": OLD}),
            "new-exploration": ("complete", {"desire_id": "wish-3", "evaluated_sources": [ref], "result": {"summary": NEW}}),
        },
        "mind_plan_runs": {
            "old-run": ("completed", {"plan_id": "plan-1", "step_id": "step-1", "actor": "creation",
                                      "result": {"summary": f"做好了 {OLD}", "verified": True, "source_id": "src_" + "2" * 32}}),
            "busy-run": ("running", {"plan_id": "plan-2", "step_id": "step-1", "actor": "creation", "result": {"summary": OLD}}),
            "new-run": ("completed", {"plan_id": "plan-3", "step_id": "step-1", "actor": "creation", "shown": [],
                                      "result": {"summary": NEW, "verified": True}}),
        },
    }
    with mind.engine.db.connect(write=True) as conn:
        for create in TABLES.values():
            conn.execute(create)
        for key, (state, data) in rows["mind_appraisals"].items():
            conn.execute("INSERT INTO mind_appraisals(id,scope,state,available,data) VALUES(?,?,?,0,?)", (key, scope, state, json.dumps(data)))
        for key, (state, data) in rows["mind_daily_reviews"].items():
            conn.execute("INSERT INTO mind_daily_reviews VALUES(?,?,?,?)", (scope, key, state, json.dumps(data)))
        for key, (state, data) in rows["mind_contacts"].items():
            conn.execute("INSERT INTO mind_contacts(id,scope,state,data) VALUES(?,?,?,?)", (key, scope, state, json.dumps(data)))
        for key, (state, data) in rows["mind_explorations"].items():
            conn.execute("INSERT INTO mind_explorations VALUES(?,?,?,?,?)", (key, scope, state, at, json.dumps(data)))
        for key, (state, data) in rows["mind_plan_runs"].items():
            conn.execute("INSERT INTO mind_plan_runs VALUES(?,?,?,?,?,?,0,'owner',0,?)",
                         (key, scope, data["plan_id"], data["step_id"], data["actor"], state, json.dumps(data)))
    return rows


def stored(mind):
    """Every process row as it is stored now, by table and key: (state, data)."""
    found = {}
    with mind.engine.db.connect() as conn:
        for table, key in (("mind_appraisals", "id"), ("mind_daily_reviews", "day"), ("mind_contacts", "id"),
                           ("mind_explorations", "id"), ("mind_plan_runs", "id")):
            found[table] = {row[0]: (row[1], json.loads(row[2])) for row in conn.execute(f"SELECT {key},state,data FROM {table}")}
    return found


def said(data):
    return json.dumps(data, ensure_ascii=False)


def test_a_process_row_that_does_not_name_what_it_was_shown_loses_its_words_to_a_delete_made_after_it(setup):
    """The rows the release before this one wrote, and a delete after them of something none of them
    names: each loses its words, and keeps its ids, its state and its receipts. A queue row still to
    be judged also loses the proposal, the revalidation and the frozen context it kept, and a seed it
    had is refused: its next attempt judges afresh. Rows at work are left to the code working them,
    rows of this release that name what they were shown keep their words, and the behaviour chain's
    own daily review, which asked no model, is left as it is. A second delete reads none of them
    again."""
    mind, source, clock = setup
    elsewhere = source("elsewhere", "别的事")
    rows = process_rows(mind, source)
    # One of them names what goes: its references take its words, and it is counted once, there.
    rows["mind_appraisals"]["old-named"] = ("complete", {"evidence_ids": [elsewhere], "proposed_result": {"reason": OLD}, "receipt": RECEIPT})
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("INSERT INTO mind_appraisals(id,scope,state,available,data) VALUES('old-named',?,'complete',0,?)",
                     (mind.scope.key(), json.dumps(rows["mind_appraisals"]["old-named"][1])))
    mind.engine.delete(elsewhere)
    now = stored(mind)
    gone = {"mind_appraisals": ("old-complete", "old-pending", "old-seed", "old-named"), "mind_daily_reviews": ("2026-09-01",),
            "mind_contacts": ("old-contact",), "mind_explorations": ("old-exploration",), "mind_plan_runs": ("old-run",)}
    for table, keys in gone.items():
        for key in keys:
            state, data = now[table][key]
            assert OLD not in said(data) and ERASED in said(data) and data[UNNAMED_MARK] is True, (table, key)
            assert state == rows[table][key][0], "its state stays"
    for table, entries in rows.items():
        for key, (state, data) in entries.items():
            if key not in gone[table]:
                assert now[table][key] == (state, data), (table, key)
    # Ids, receipts and what a run proves stay.
    complete = now["mind_appraisals"]["old-complete"][1]
    assert complete["receipt"] == RECEIPT and complete["evidence_ids"] == rows["mind_appraisals"]["old-complete"][1]["evidence_ids"]
    assert complete["result"] == {"event_id": "mind_old"} and complete["evaluated_sources"][0]["source_id"]
    run = now["mind_plan_runs"]["old-run"][1]
    assert run["result"]["verified"] is True and run["result"]["source_id"] == "src_" + "2" * 32 and run["plan_id"] == "plan-1"
    exploration = now["mind_explorations"]["old-exploration"][1]
    assert exploration["source_id"] == "src_" + "1" * 32 and exploration["desire_id"] == "wish-1"
    assert now["mind_contacts"]["old-contact"][1]["draft_receipt"] == RECEIPT
    assert now["mind_daily_reviews"]["2026-09-01"][1]["receipt"] == RECEIPT
    # Still to be judged: nothing is taken up again, whatever it was.
    pending = now["mind_appraisals"]["old-pending"][1]
    assert not {"reuse", "tier", "revalidation", "frozen_memory_context"} & set(pending)
    assert pending["error_detail"]["code"] == "revision-moved" and pending["error_detail"]["message"] == ERASED
    seed = now["mind_appraisals"]["old-seed"][1]
    assert seed["seed_rejected"] is True and seed["seed_receipt"] == RECEIPT
    for key in ("old-pending", "old-seed"):
        assert candidate(now["mind_appraisals"][key][1], True) is None, key
    # The delete counts them by table.
    with mind.engine.db.connect() as conn:
        [first] = [json.loads(row[0]) for row in conn.execute("SELECT data FROM metrics WHERE name='memory_erased' ORDER BY rowid")]
    assert {key: value for key, value in first.items() if key.startswith("unnamed:")} == {
        "unnamed:mind_appraisals": 3, "unnamed:mind_daily_reviews": 1, "unnamed:mind_contacts": 1,
        "unnamed:mind_explorations": 1, "unnamed:mind_plan_runs": 1}
    assert first["mind_appraisals"] == 1 and OLD not in said(now["mind_appraisals"]["old-named"][1])
    # Done: the next delete changes none of them, and counts none.
    mind.engine.delete(source("another", "又一件别的事"))
    assert stored(mind) == now
    with mind.engine.db.connect() as conn:
        last = json.loads(conn.execute("SELECT data FROM metrics WHERE name='memory_erased' ORDER BY rowid DESC LIMIT 1").fetchone()[0])
    assert not [key for key in last if key.startswith("unnamed:")]


def test_reerase_counts_the_unnamed_rows_by_table_and_takes_them_all_rows_left_at_work_included(setup, monkeypatch):
    """A store the release before this one wrote: its rows, and a delete it made -- the tombstone
    and nothing more. Before any delete the dry run counts nothing: no delete, nothing to take. After
    it, the dry run counts the rows by table among the layers, those left at work as well, since
    nothing runs in the stopped window; the apply takes what it counted, and a second dry run finds
    nothing left (CL6D-MM-04)."""
    mind, source, clock = setup
    engine = mind.engine
    gone_before = source("gone-before", "发布之前删掉的一条")
    rows = process_rows(mind, source)
    with engine.db.connect(write=True) as conn:
        conn.execute("INSERT INTO mind_appraisals(id,scope,state,available,data) VALUES('old-named',?,'complete',0,?)",
                     (mind.scope.key(), json.dumps({"evidence_ids": [gone_before], "proposed_result": {"reason": OLD}, "receipt": RECEIPT})))
    root = engine.db.root
    unnamed = lambda layers: {key: value for key, value in layers.items() if key.startswith("unnamed:")}
    assert unnamed(repair.run(root, steps=("reerase",))["steps"]["reerase"]["plan"]["layers"]) == {}
    # A delete as the release before this one made it.
    monkeypatch.setattr(erasure, "erase", lambda *args, **kwargs: {})
    monkeypatch.setattr(erasure, "queue_history", lambda *args, **kwargs: None)
    engine.delete(gone_before)
    monkeypatch.undo()
    assert OLD in said(stored(mind)), "the old delete took no words"
    counts = {"unnamed:mind_appraisals": 4, "unnamed:mind_daily_reviews": 1, "unnamed:mind_contacts": 2,
              "unnamed:mind_explorations": 2, "unnamed:mind_plan_runs": 2}
    plan = repair.run(root, steps=("reerase",))["steps"]["reerase"]["plan"]
    # The row that names the delete is counted by its references, once.
    assert unnamed(plan["layers"]) == counts and plan["layers"]["mind_appraisals"] == 1
    assert plan["derived_rows"] >= sum(counts.values()) + 1
    done = repair.run(root, apply=True, steps=("reerase",))["steps"]["reerase"]["done"]
    assert unnamed(done["layers"]) == counts
    now = stored(mind)
    for table, entries in rows.items():
        for key, (state, data) in entries.items():
            if key.startswith("new-") or key in {"2026-09-02", "2026-09-03"}:
                assert now[table][key] == (state, data), (table, key)
            else:
                assert OLD not in said(now[table][key][1]) and now[table][key][0] == state, (table, key)
    assert OLD not in said(now["mind_appraisals"]["old-named"][1]) and now["mind_appraisals"]["old-named"][1][UNNAMED_MARK] is True
    again = repair.run(root, steps=("reerase",))["steps"]["reerase"]["plan"]
    assert unnamed(again["layers"]) == {} and again["derived_rows"] == 0


def test_the_guide_and_the_repair_say_which_process_rows_lose_their_words_to_any_delete():
    from pathlib import Path

    guide = " ".join((Path(__file__).resolve().parents[2] / "docs" / "operations.md").read_text(encoding="utf-8").split())
    assert "A process row that does not name what its model or executor was shown at all" in guide
    assert "loses its words to any delete, its ids, state and receipts kept" in guide and "`unnamed:<table>`" in guide
    assert "`unnamed:<table>`" in " ".join((repair.__doc__ or "").split())
    assert set(erasure.UNNAMED) == {"mind_appraisals", "mind_daily_reviews", "mind_contacts", "mind_explorations", "mind_plan_runs"}
