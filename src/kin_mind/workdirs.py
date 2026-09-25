"""What an exploration's working directory keeps once its run is settled (CL6-MM-07).

The directory holds the brief the run was handed (`input.json`: the evidence, the recent dialogue,
the earlier results and histories, in their words), the checkpoint it continued from and the one
it left (`continuation.json`, `checkpoint.json`), each attempt's final answer (`result-N.json`), its
receipt (`receipt.json`) and the ledgers of what it observed. By the time the run is settled all of
it that matters is in the store -- the report, the checkpoint a later attempt continues from, the
observations, the receipts -- where a delete reaches it. The files are copies no delete reaches.

So once a run is settled, a stopped one too, these files keep what names or classifies something
-- identifiers, states and codes, model and executor names, paths and locators, times, hashes,
numbers -- and lose every word. The directory itself stays, as a stopped run's always has. Only
words go: nothing a person wrote or asked for is deleted here, and the store keeps its own."""
from __future__ import annotations

import json
import re
from pathlib import Path

from eventmem.core.db import dumps
from eventmem.paths import atomic_write

ERASED = "[已删除]"
# A string that stays names or classifies something: no space, nothing outside ASCII, no query
# string (`?`, `&` may carry the words of a search), and not long.
KEPT = re.compile(r"[A-Za-z0-9_.:/@#%+=,~-]{0,256}")
# The files of the directory that carry words; `result-N.json` beside them, one an attempt.
FILES = ("input.json", "continuation.json", "checkpoint.json", "receipt.json",
         "computer-observations.json", "computer-use-observations.json", "web-observations.json")
RESULTS = "result-*.json"
# Written once the directory has lost its words, so that a later sweep passes it by.
MARKER = "words-removed.json"


def without_words(value):
    """`value` with every string that does not name or classify something replaced by ERASED; its
    keys, numbers, booleans and nulls as they were."""
    if isinstance(value, dict):
        return {key: without_words(item) for key, item in value.items()}
    if isinstance(value, list):
        return [without_words(item) for item in value]
    if isinstance(value, str):
        return value if KEPT.fullmatch(value) else ERASED
    return value


def scrub_file(path):
    """One file without its words: JSON keeps its shape, anything else keeps nothing. True when the
    file changed; a file already without words is left alone."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, IsADirectoryError):
        return False
    try:
        value = json.loads(text)
    except ValueError:
        value = ERASED
        kept = ERASED
    else:
        kept = without_words(value)
        if kept == value:
            return False
    atomic_write(path, dumps(kept))
    return True


def scrub(directory, *, state=None):
    """A settled run's directory without the words of its files, marked so. Returns the names of
    the files that changed. A directory that is not there has nothing to lose."""
    directory = Path(directory)
    if not directory.is_dir():
        return []
    changed = [name for name in FILES if scrub_file(directory / name)]
    changed += [path.name for path in sorted(directory.glob(RESULTS)) if scrub_file(path)]
    atomic_write(directory / MARKER, dumps({"state": state if isinstance(state, str) and KEPT.fullmatch(state) else None,
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
