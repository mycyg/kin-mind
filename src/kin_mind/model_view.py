"""What a model is shown of what Kin keeps: every evidence reference as its trace, every source as
the source it is, and never a source's metadata.

**The owner's decision (2026-09-28).** 来源的元数据只做为索引检索，不应该注入哈，也不必要引用，只需要可以溯源就行
-- a source's metadata is an index for retrieval. It is not put into anything a model is shown and
is not cited; what is shown only has to stay traceable, by its ids.

**References** (`evidence_refs.is_ref`: a dict with a string `source_id`, `record_id` and `hash`) are
shown as `evidence_refs.trace` gives them -- whatever they carry besides `evidence_refs.DROPPED` --
whether the document that holds them is slimmed or not: a full reference and a slim one are shown
alike. What a reference carries besides the trace and the dropped fields (an exploration target's
`exploration_id`, a tombstone's `erased`) stays.

**Sources shown as items** (`is_source`: a dict whose `id` is a source id and which carries a
`metadata`, as the sources under review in an appraisal do) are shown without their `metadata`. The
facts a prompt reads from it are shown under their own names beside the source (`SOURCE_FACTS`):
the host event it records (`host_event`), who spoke (`role`) and the exploration whose result it is
(`exploration_id`). Everything else in it -- the pages a result read with their titles, locators and
excerpts, the models and receipts of a run, a tool's arguments -- is an index for retrieval. A fact
the source already names at the top is left as the source names it.

**A record's copy.** A root record -- the one the engine made of a whole source when it received it
-- was given that source's metadata as its `attributes`. Where the boundary can read the sources
(the two MCP servers, `source_metadata`), a root record is shown without what it copied: its
attributes keep the named facts, what the engine stamped (`origin_kind`), and whatever a correction,
an extraction or a later step gave it that is not the source's own.

**Text.** A string that is JSON, or has JSON lines, and holds one of those is shown with them as
above, re-serialized (`eventmem.core.db.dumps`); any other string is shown exactly as it is. Only a
string that names one of the dropped fields is ever parsed, so a text is untouched unless it carries
what this takes out.

**Where it is enforced**, one place for each boundary a model is on the other side of:

* the core MCP server (`eventmem.core.mcp.create_mcp`): every tool's result (`guard_tools`), root
  records included;
* the host's memory server (host `kin_memory_mcp.scoped_tool`): every tool's result, root records
  included;
* the host's actions whose answers the host puts in front of a model (`kin_mind.host.MODEL_FACING`:
  the memory context, the state read, the contact candidate and claim, a creation's claim and brief,
  the session checkpoint, the history and graph reads);
* a prepared context injection (`ContextDelivery.prepare`), before its text is hashed;
* every DeepSeek request: `DeepSeek.structured`, the appraisal's own request (`DeepSeek._post`, what
  the user turns carry, the recall reads' results among them) and its projection (`appraisal_context`),
  a main-session fork's (`NativeReview._native`) and eventmem's providers (`Providers._json_once`);
* the exploration executor's prompt and the input files it is handed (`codex_executor`).

`leaks` finds what would still be taken out; the tests use it at each boundary.
"""

from __future__ import annotations

import functools
import inspect
import json
import re

from eventmem.core.db import digest, dumps

from .evidence_refs import DROPPED, is_ref, trace

SOURCE_ID = re.compile(r"src_[0-9a-f]{32}")
RECORD_ID = re.compile(r"mem_[0-9a-f]{32}")
# The facts of a source's metadata a prompt reads, shown under their own names beside the source.
SOURCE_FACTS = ("host_event", "role", "exploration_id")
# A string is parsed only if it names one of these: nothing else can hold what this takes out. Bare
# words, so JSON carried as text inside JSON (a memory item's line: `"text":"{\"evidence\":...}"`)
# is found at any depth.
MARKS = tuple(sorted({*DROPPED, "metadata"}))
# Deeper than this, a value is shown as it is: nothing Kin keeps nests so deep, and a text a model
# wrote as brackets must not stop a read.
DEPTH = 200


def is_source(value):
    """A source shown as an item: its `id` is a source's and it carries a `metadata`. A reference is
    not one (it names its source by `source_id`)."""
    return (isinstance(value, dict) and not is_ref(value) and isinstance(value.get("id"), str)
            and SOURCE_ID.fullmatch(value["id"]) is not None and "metadata" in value)


def is_root_record(value):
    """A record the engine made of a whole source when it received it (`Engine.receive`: its id is
    the source's root id), shown with its `attributes`."""
    if not (isinstance(value, dict) and isinstance(value.get("id"), str) and RECORD_ID.fullmatch(value["id"])
            and isinstance(value.get("attributes"), dict) and isinstance(value.get("source_ids"), list)
            and value["source_ids"] and isinstance(value["source_ids"][0], str)):
        return False
    return value["id"] == "mem_" + digest([value["source_ids"][0], "root"])[:32]


def source_metadata(engine):
    """What a boundary that can read the store looks a source's metadata up with, for `for_model`:
    the metadata the source row holds (None when there is no row), each source read once."""
    found = {}

    def lookup(source_id):
        if source_id not in found:
            with engine.db.connect() as conn:
                row = conn.execute("SELECT data FROM sources WHERE id=?", (source_id,)).fetchone()
            data = json.loads(row[0]) if row else None
            found[source_id] = data.get("metadata") if isinstance(data, dict) and isinstance(data.get("metadata"), dict) else None
        return found[source_id]
    return lookup


def source_facts(metadata):
    """The facts of a source's metadata a prompt reads (`SOURCE_FACTS`), those it has."""
    if not isinstance(metadata, dict):
        return {}
    return {key: metadata[key] for key in SOURCE_FACTS
            if isinstance(metadata.get(key), (str, int, float, bool)) and metadata[key] != ""}


def index_only(container, key):
    """Whether `key` of `container` is something `for_model` takes out: a reference's dropped field
    or a shown source's metadata. What a model is shown never holds it, so an id found only there is
    not what a read of it rests on (host `kin_memory_mcp.rests_on`)."""
    return (key in DROPPED and is_ref(container)) or (key == "metadata" and is_source(container))


def for_model(value, *, metadata=None):
    """`value` as a model may be shown it (see the module docstring). `metadata`, where the caller can
    read the store (`source_metadata`), lets a root record be shown without its copy of its source's
    metadata. A copy where anything changes; the original is left as it is."""
    return _walk(value, 0, metadata)


def _record(value, metadata):
    copied = metadata(value["source_ids"][0])
    if not copied:
        return value
    kept = {key: item for key, item in value["attributes"].items()
            if key in SOURCE_FACTS or key not in copied or copied[key] != item}
    return {**value, "attributes": kept}


def _walk(value, depth, metadata=None):
    if depth > DEPTH:
        return value
    if isinstance(value, dict):
        if is_ref(value):
            value = trace(value)
        elif is_source(value):
            shown = {key: item for key, item in value.items() if key != "metadata"}
            for key, fact in source_facts(value.get("metadata")).items():
                shown.setdefault(key, fact)
            value = shown
        elif metadata is not None and is_root_record(value):
            value = _record(value, metadata)
        return {key: _walk(item, depth + 1, metadata) for key, item in value.items()}
    if isinstance(value, list):
        return [_walk(item, depth + 1, metadata) for item in value]
    if isinstance(value, tuple):
        return tuple(_walk(item, depth + 1, metadata) for item in value)
    if isinstance(value, str):
        return _text(value, depth, metadata)
    return value


def _parsed(text):
    try:
        found = json.loads(text)
    except (ValueError, RecursionError):
        return None
    return found if isinstance(found, (dict, list)) else None


def _text(text, depth, metadata=None):
    if not any(mark in text for mark in MARKS) and not (metadata is not None and "attributes" in text):
        return text
    if text.lstrip()[:1] in ("{", "["):
        whole = _parsed(text)
        if whole is not None:
            shown = _walk(whole, depth + 1, metadata)
            return text if shown == whole else dumps(shown)
    lines, changed = text.split("\n"), False
    for index, line in enumerate(lines):
        if line.lstrip()[:1] not in ("{", "[") or not (any(mark in line for mark in MARKS)
                                                       or metadata is not None and "attributes" in line):
            continue
        part = _parsed(line)
        if part is None:
            continue
        shown = _walk(part, depth + 1, metadata)
        if shown != part:
            lines[index], changed = dumps(shown), True
    return "\n".join(lines) if changed else text


def leaks(value, path=()):
    """Where `value` still holds what `for_model` takes out: a reference with a dropped field, a
    source with its metadata -- in JSON carried as text too, whole or by line. Paths, for a test to
    name what leaked."""
    found = []

    def walk(node, where, depth):
        if depth > DEPTH:
            return
        if isinstance(node, dict):
            if is_ref(node) and set(DROPPED) & set(node):
                found.append((*where, "<reference>"))
            elif is_source(node):
                found.append((*where, "<source metadata>"))
            for key, item in node.items():
                walk(item, (*where, key), depth + 1)
        elif isinstance(node, (list, tuple)):
            for index, item in enumerate(node):
                walk(item, (*where, index), depth + 1)
        elif isinstance(node, str) and any(mark in node for mark in MARKS):
            whole = _parsed(node) if node.lstrip()[:1] in ("{", "[") else None
            parts = [whole] if whole is not None else [_parsed(line) for line in node.split("\n")
                                                       if line.lstrip()[:1] in ("{", "[")]
            for index, part in enumerate(parts):
                if part is not None:
                    walk(part, (*where, f"<text {index}>"), depth + 1)

    walk(value, tuple(path), 0)
    return found


def shown_by(function, engine=None):
    """`function` with its result shown as `for_model` shows it -- root records too, given the
    `engine` whose sources they copied -- and the signature it was declared with (evaluated, as an MCP
    server reads it)."""
    signature = inspect.signature(function, eval_str=True)

    def show(result):
        return for_model(result, metadata=source_metadata(engine) if engine is not None else None)
    if inspect.iscoroutinefunction(function):
        @functools.wraps(function)
        async def shown(*args, **kwargs):
            return show(await function(*args, **kwargs))
    else:
        @functools.wraps(function)
        def shown(*args, **kwargs):
            return show(function(*args, **kwargs))
    shown.__signature__ = signature
    shown.__annotations__ = {key: parameter.annotation for key, parameter in signature.parameters.items()}
    shown.__annotations__["return"] = signature.return_annotation
    shown.shown_to_model = True
    return shown


def guard_tools(server, engine=None):
    """Every tool `server` (a FastMCP server) has registered, re-registered so its result is shown as
    `for_model` shows it, root records of `engine` included. Its name, title, description and
    annotations stay."""
    for tool in list(server._tool_manager.list_tools()):
        if getattr(tool.fn, "shown_to_model", False):
            continue
        server.remove_tool(tool.name)
        server.add_tool(shown_by(tool.fn, engine), name=tool.name, title=tool.title, description=tool.description,
                        annotations=tool.annotations, icons=tool.icons, meta=tool.meta)
    return server
