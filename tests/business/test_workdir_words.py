"""A settled exploration's working directory keeps its files without their words (CL6-MM-07).

The brief (`input.json`), each answer (`result-N.json`), the checkpoints and the receipt are copies
of what is in the store by the time the run is settled; a delete reaches the store and not them.
Once the run is settled, a stopped one too, they keep what names or classifies something --
identifiers, states, codes, times, hashes, numbers, locators -- and lose every word. The directory
stays. A run stopped with the host, or settled before this release, is found by the next run."""
import json
from pathlib import Path

from eventmem.core.db import dumps

from kin_mind.workdirs import ERASED, MARKER, scrub, without_words
from kin_mind.memory import MemoryContinuity
from test_erasure import settle

pytest_plugins = ('test_kin_mind', 'test_autonomous_plans')

MARKER_WORDS = "quillmere"


def files_of(directory):
    return {path.name: path.read_text(encoding="utf-8") for path in Path(directory).iterdir() if path.is_file()}


def test_what_names_or_classifies_something_stays_and_words_go():
    """Ids, codes, names, paths, locators, times, hashes and numbers stay; a sentence, anything not
    ASCII and a query string go; keys and shapes stay."""
    value = {"id": "explore_" + "a" * 32, "state": "timed-out", "model": "deepseek-flash", "attempt": 2, "partial": True,
             "at": "2026-09-26T01:02:03.456+00:00", "path": "/Users/kin/state/exploration/x/result-1.json",
             "url": "memory://src_" + "b" * 32, "page": "https://example.org/a/b", "search": "https://example.org/s?q=her+cat",
             "sha256": "c" * 64, "summary": "They walked the harbour.", "note": f"她说 {MARKER_WORDS}", "one": "harbour",
             "findings": [f"{MARKER_WORDS} is near the river", {"source_id": "src_" + "d" * 32, "title": "A title"}], "none": None}
    kept = without_words(value)
    assert kept["id"] == value["id"] and kept["state"] == "timed-out" and kept["attempt"] == 2 and kept["partial"] is True
    assert kept["at"] == value["at"] and kept["path"] == value["path"] and kept["url"] == value["url"] and kept["page"] == value["page"]
    assert kept["sha256"] == value["sha256"] and kept["none"] is None and kept["one"] == "harbour"
    assert kept["summary"] == ERASED and kept["note"] == ERASED and kept["search"] == ERASED
    assert kept["findings"] == [ERASED, {"source_id": "src_" + "d" * 32, "title": ERASED}]
    assert without_words(kept) == kept


def test_a_settled_exploration_leaves_its_directory_without_words_and_the_next_run_finds_the_ones_left(env, tmp_path):
    """The run writes its brief, answer, checkpoints and receipt into its directory as the executor
    does; the brief holds the words of a message. Settled, every file keeps its ids and states and
    no word, and says so. A directory a stopped run left, and one settled before this release, lose
    theirs at the next run; one still running, and one no run is named for, are left alone."""
    from kin_mind.exploration import Explorations
    from test_autonomous_plans import create, decide

    mind, plans, _, _, initial = env
    MemoryContinuity(mind).ingest({"id": "said-before", "kind": "owner-message", "at": mind.clock(),
                                   "text": f"那家店好像叫 {MARKER_WORDS}"})
    root = tmp_path / "exploration"
    decide(env, create(env, actor="explore"))
    plans.sync_wishes()
    written = {}

    def runner(executable, brief, directory, **kwargs):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        assert MARKER_WORDS in json.dumps(brief, ensure_ascii=False), "the brief carried the message"
        answer = {"summary": f"查到了 {MARKER_WORDS} 的来历", "findings": [f"{MARKER_WORDS} 开业于 1990 年"],
                  "sources": [{"url": "memory://" + initial, "title": "Owner note"}], "open_questions": [], "suggested_share": None}
        (directory / "input.json").write_text(dumps(brief))
        (directory / "result-1.json").write_text(dumps(answer))
        (directory / "checkpoint.json").write_text(dumps({"exploration_id": directory.name, "attempt": 1, "state": "complete",
                                                          "partial_findings": answer}))
        (directory / "receipt.json").write_text(dumps({"state": "complete", "attempt": 1, "executor": "codex-cli", "result": answer,
                                                       "workdir": str(directory), "usage": {"status": "reported"}}))
        written["directory"] = directory
        return {"state": "complete", "partial": False, "attempt": 1, "result": answer}

    ran = Explorations(mind).run("codex", str(root), "planning-v1", runner=runner)
    settle(mind.engine)
    assert ran["state"] == "complete"
    directory = written["directory"]
    assert directory.name == ran["id"] and directory.is_dir(), "the directory stays"
    found = files_of(directory)
    assert MARKER_WORDS not in json.dumps(found, ensure_ascii=False) and "查到了" not in json.dumps(found, ensure_ascii=False)
    receipt = json.loads(found["receipt.json"])
    assert receipt["state"] == "complete" and receipt["executor"] == "codex-cli" and receipt["result"]["summary"] == ERASED
    assert json.loads(found["result-1.json"])["sources"][0]["url"] == "memory://" + initial, "a locator stays"
    assert json.loads(found["input.json"])["source_ids"], "the brief keeps what it names"
    assert json.loads(found[MARKER])["state"] == "complete"
    # The store keeps the report, where a delete reaches it.
    assert MARKER_WORDS in mind.engine.source(ran["source_id"], content=True).read_text()

    # What the host left behind: a stopped run, a run settled by the last release, a run still going,
    # and a directory no run is named for.
    left = {"explore_stopped": "interrupted", "explore_old": "failed", "explore_going": "running"}
    with mind.engine.db.connect(write=True) as conn:
        for eid, state in left.items():
            conn.execute("INSERT INTO mind_explorations VALUES(?,?,?,?,?)", (eid, mind.scope.key(), state, mind.clock(), dumps({})))
    for name in [*left, "deploy-check-run"]:
        (root / name).mkdir(parents=True)
        (root / name / "input.json").write_text(dumps({"question": f"去看 {MARKER_WORDS}", "source_ids": [initial]}))
        (root / name / "result-2.txt.json").write_text(f"not json: {MARKER_WORDS}")
    Explorations(mind).run("codex", str(root), "planning-v1", runner=runner)
    for name in ("explore_stopped", "explore_old"):
        found = files_of(root / name)
        assert MARKER_WORDS not in json.dumps(found, ensure_ascii=False), name
        assert json.loads(found["input.json"]) == {"question": ERASED, "source_ids": [initial]}
        assert json.loads(found["result-2.txt.json"]) == ERASED
    for name in ("explore_going", "deploy-check-run"):
        assert MARKER_WORDS in (root / name / "input.json").read_text(), name
    assert scrub(root / "explore_old") == [], "a second pass finds nothing left"
