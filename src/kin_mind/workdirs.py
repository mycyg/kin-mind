"""What an exploration's working directory keeps once its run is settled (CL6-MM-07, CL7B-MM-05).

The directory holds the brief the run was handed (`input.json`: the evidence, the recent dialogue,
the earlier results and histories, in their words), the checkpoint it continued from and the one
it left (`continuation.json`, `checkpoint.json`), each attempt's final answer (`result-N.json`), its
receipt (`receipt.json`), the ledgers of what it observed, the text of every page its web reader
fetched (`web-content/`), and the file reader's settings, which carry the earlier observations it
was told of. By the time the run is settled all of it that matters is in the store -- the report,
the checkpoint a later attempt continues from, the observations, the receipts -- where a delete
reaches it. The files are copies no delete reaches, and nothing reads them after the run.

So once a run is settled, a stopped one too, these files keep what names or classifies something
-- identifiers, states and codes, model and executor names, paths and locators, times, hashes,
numbers -- and lose every word (`without_words`): a page's text goes whole. The directory itself
stays, as a stopped run's always has. Only words go: nothing a person wrote or asked for is
deleted here, and the store keeps its own.

The same rule, word for word, is adapters/without-words.mjs, which applies it to a creation's final
answer and to the host's status file; tests/business/helpers/without-words-cases.json holds the
cases both answer the same."""
from __future__ import annotations

import json
import re
from pathlib import Path

from eventmem.core.db import dumps
from eventmem.paths import atomic_write

ERASED = "[已删除]"
# A string that may stay: no space, nothing outside ASCII, no query string (`?`, `&`) and no
# percent-encoding (`%`) -- either carries words of its own -- and not long.
KEPT = re.compile(r"[A-Za-z0-9_.:/@#+=,~-]{0,256}")
# One word, letters alone, whatever separators stand at its ends.
WORD = re.compile(r"[_.:/@#+=,~-]*[A-Za-z]+[_.:/@#+=,~-]*")
# An email address, wherever it stands in the string, names a person.
EMAIL = re.compile(r"[A-Za-z0-9_.+=~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
# The fields whose value is a state, a code or a name that the host, the mind, an executor or its
# tools give -- never a model's or a person's word. A single word stays in one of these, or where
# it is the name of a key of the same document; anywhere else it is a word. A code field missing
# here loses a one-word value to ERASED, which costs a file read by nobody some detail; `state` is
# here because the host's health and audit read it from the status file, and a receipt's
# `truncated_by` and `usage_status` because an operator reads there why a fork's reads were cut
# short and whether a call's usage was reported (CL8-MM-04).
CODE_KEYS = frozenset({
    "state", "status", "result_state", "stage", "outcome",
    "channel", "provider", "model", "reasoning", "executor", "backend", "adapter", "server", "tool",
    "kind", "type", "item_type", "content_type", "class", "code", "category", "error", "error_tags",
    "retry_condition", "authority", "basis", "actor", "origin",
    "tier", "lane", "stimulus", "waiting_reason", "repair_reason", "reason_withheld",
    "truncated_by", "usage_status",
})
# The fields that hold a code where the host writes them and a model's own word elsewhere: only their
# codes stay. `role` is whose turn a message is, and in a graph relation what the model called
# someone (erasure.py, CL6-MM-09) (CL8-MM-06).
CODE_VALUES = {"role": frozenset({"user", "assistant", "system", "tool", "developer"})}
# The files of the directory that carry words, beside `result-N.json`, one an attempt.
FILES = ("input.json", "continuation.json", "checkpoint.json", "receipt.json",
         "computer-observations.json", "computer-use-observations.json", "web-observations.json",
         "computer-reader.json")
RESULTS = "result-*.json"
# The text of each page the web reader fetched: text whole, never a structure of ours.
PAGES = "web-content/*.txt"
# What a process that died between writing and renaming left beside a ledger or a page.
LEFT = ("*.tmp", "web-content/*.tmp")
# Written once the directory has lost its words, so that a later sweep passes it by.
MARKER = "words-removed.json"


def kept(value, key=None, keys=frozenset()):
    """Whether the string `value` names or classifies something (the module's rule), as it stands
    under `key` in a document whose keys are `keys`."""
    if not KEPT.fullmatch(value) or EMAIL.search(value):
        return False
    if WORD.fullmatch(value):
        return key in CODE_KEYS or value in keys or value in CODE_VALUES.get(key, ())
    return True


def keys_of(value, found=None):
    """Every key of the document `value`, at any depth."""
    found = set() if found is None else found
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                found.add(key)
            keys_of(item, found)
    elif isinstance(value, list):
        for item in value:
            keys_of(item, found)
    return found


def without_words(value):
    """`value` with every string that does not name or classify something replaced by ERASED; its
    keys, numbers, booleans and nulls as they were."""
    keys = frozenset(keys_of(value))

    def walk(item, key):
        if isinstance(item, dict):
            return {name: walk(inner, name) for name, inner in item.items()}
        if isinstance(item, list):
            return [walk(inner, key) for inner in item]
        if isinstance(item, str):
            return item if kept(item, key, keys) else ERASED
        return item

    return walk(value, None)


def scrub_file(path, *, whole=False):
    """One file without its words: JSON keeps its shape, anything else keeps nothing, and a file
    that is text whole (`whole`: a fetched page) keeps nothing whatever it looks like. True when
    the file changed; a file already without words is left alone."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, IsADirectoryError):
        return False
    try:
        if whole:
            raise ValueError
        value = json.loads(text)
    except ValueError:
        if text == dumps(ERASED):
            return False
        left = ERASED
    else:
        left = without_words(value)
        if left == value:
            return False
    atomic_write(path, dumps(left))
    return True


def scrub(directory, *, state=None):
    """A settled run's directory without the words of its files, marked so. Returns the names of
    the files that changed. A directory that is not there has nothing to lose."""
    directory = Path(directory)
    if not directory.is_dir():
        return []
    changed = [name for name in FILES if scrub_file(directory / name)]
    for pattern in (RESULTS, *LEFT):
        changed += [str(path.relative_to(directory)) for path in sorted(directory.glob(pattern)) if scrub_file(path)]
    changed += [str(path.relative_to(directory)) for path in sorted(directory.glob(PAGES)) if scrub_file(path, whole=True)]
    atomic_write(directory / MARKER, dumps({"state": state if isinstance(state, str) and kept(state, "state") else None,
                                           "files": sorted(changed)}))
    return changed


def sweep(root, settled):
    """Every directory under `root` named for a settled run (`settled`: run id to its state) and not
    yet marked loses its words: a run stopped with the host, or settled before this release, is
    found at the next start. A directory no settled run is named for is left alone."""
    root = Path(root)
    if not root.is_dir():
        return []
    done = []
    for directory in sorted(root.iterdir()):
        if directory.name in settled and directory.is_dir() and not (directory / MARKER).exists():
            scrub(directory, state=settled[directory.name])
            done.append(directory.name)
    return done
