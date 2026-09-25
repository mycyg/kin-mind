"""A settled exploration's working directory keeps its files without their words (CL6-MM-07,
CL7B-MM-05).

The brief (`input.json`), each answer (`result-N.json`), the checkpoints, the receipt, the ledgers,
the pages the web reader fetched and the file reader's settings are copies of what is in the store
by the time the run is settled; a delete reaches the store and not them. Once the run is settled, a
stopped one too, they keep what names or classifies something -- identifiers, states, codes, times,
hashes, numbers, locators -- and lose every word: a sentence, anything outside ASCII, a query
string, percent-encoding, an email address, and a single word unless a field of codes holds it or
it names a key of the same document. The directory stays. A run stopped with the host, or settled
before this release, is found by the next run. The rule is the same, case for case, as
adapters/without-words.mjs (helpers/without-words-cases.json)."""
import json
import re
from pathlib import Path

import pytest
from eventmem.core.db import dumps

from kin_mind.workdirs import CODE_KEYS, CODE_VALUES, ERASED, MARKER, scrub, without_words
from kin_mind.memory import MemoryContinuity
from test_erasure import settle

pytest_plugins = ('test_kin_mind', 'test_autonomous_plans')

MARKER_WORDS = "quillmere"


def files_of(directory):
    return {str(path.relative_to(directory)): path.read_text(encoding="utf-8")
            for path in sorted(Path(directory).rglob("*")) if path.is_file()}


CASES = json.loads((Path(__file__).parent / "helpers" / "without-words-cases.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_what_names_or_classifies_something_stays_and_words_go(case):
    """Each shared case, as adapters/without-words.mjs answers it too; a second pass changes nothing."""
    assert without_words(case["value"]) == case["expected"]
    assert without_words(case["expected"]) == case["expected"]


def test_both_sides_know_the_same_fields_of_codes():
    """The fields whose one-word values stay are the same list on both sides, and so are the fields
    that keep only their codes, with the same codes (CL8-MM-04, CL8-MM-06)."""
    source = (Path(__file__).resolve().parents[2] / "adapters" / "without-words.mjs").read_text(encoding="utf-8")
    listed = re.search(r"export const CODE_KEYS=Object\.freeze\(\[(.*?)\]\);", source, re.S).group(1)
    assert set(re.findall(r"'([a-z_]+)'", listed)) == set(CODE_KEYS)
    assert {"truncated_by", "usage_status"} <= CODE_KEYS and "role" not in CODE_KEYS
    valued = re.search(r"export const CODE_VALUES=Object\.freeze\(\{(.*?)\}\);", source, re.S).group(1)
    both = {key: set(re.findall(r"'([a-z_]+)'", codes)) for key, codes in re.findall(r"(\w+):Object\.freeze\(\[(.*?)\]\)", valued)}
    assert both == {key: set(codes) for key, codes in CODE_VALUES.items()} and both["role"] >= {"user", "assistant"}


def test_a_settled_exploration_leaves_its_directory_without_words_and_the_next_run_finds_the_ones_left(env, tmp_path):
    """The run writes its brief, answer, checkpoints, receipt, web ledger, the page it fetched and
    the file reader's settings into its directory as the executor and its tools do; the brief holds
    the words of a message, the ledger what it searched for and where, the settings the title of an
    earlier observation. Settled, every file keeps its ids, states and plain locators and no word --
    a percent-encoded locator and a one-word search go too, the page whole -- and says so. A
    directory a stopped run left, and one settled before this release, lose theirs at the next run;
    one still running, and one no run is named for, are left alone."""
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
        page = "web_" + "e" * 31 + "1"
        (directory / "web-observations.json").write_text(dumps({page: {
            "evidence_id": page, "state": "observed", "tool": "read_page", "query": MARKER_WORDS, "title": MARKER_WORDS.title(),
            "locator": "https://zh.wikipedia.org/wiki/%E9%82%A3%E5%AE%B6%E5%BA%97", "requested_locator": "https://example.org/a/b",
            "content_ref": f"web-content/{page}.txt", "contact": "owner@example.com"}}))
        # What the page said; JSON as it happens, and with ids of its own: text all the same.
        (directory / "web-content").mkdir()
        (directory / "web-content" / f"{page}.txt").write_text(dumps({"slug": f"{MARKER_WORDS}-1990", "id": "mem_" + "f" * 32}))
        (directory / "computer-reader.json").write_text(dumps({"roots": ["/Users/kin/Notes"], "previous": [
            {"id": "computer_" + "d" * 31 + "2", "locator": "/Users/kin/Notes/x1.txt", "version": "c0" * 32,
             "title": MARKER_WORDS.upper(), "observed_at": mind.clock()}]}))
        # A ledger a tool died writing.
        (directory / "web-observations.tmp").write_text(dumps({"query": MARKER_WORDS}))
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
    observed = next(iter(json.loads(found["web-observations.json"]).values()))
    assert observed["state"] == "observed" and observed["tool"] == "read_page" and observed["requested_locator"] == "https://example.org/a/b"
    assert observed["query"] == observed["title"] == observed["locator"] == observed["contact"] == ERASED
    assert json.loads(found["web-content/web_" + "e" * 31 + "1.txt"]) == ERASED, "a page is text whole"
    reader = json.loads(found["computer-reader.json"])
    assert reader["previous"][0]["id"] == "computer_" + "d" * 31 + "2" and reader["previous"][0]["title"] == ERASED
    assert json.loads(found["web-observations.tmp"]) == {"query": ERASED}
    marker = json.loads(found[MARKER])
    assert marker["state"] == "complete" and {"web-content/web_" + "e" * 31 + "1.txt", "computer-reader.json"} <= set(marker["files"])
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
