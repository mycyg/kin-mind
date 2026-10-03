"""The exploration source ledger: what a run may cite, and the proof behind it.

Layered states, never prefix matching. A locator is citable only when an explicit
receipt attests it: read this run (`observed`), or host-supplied / previously
verified material (`historical`, not re-verified this run). A URL merely present
in the question is `requested`; a search hit is `search_result` — visible, not
read. Failed and superseded reads are never citable. input.json proves only what
the host provided; it never vouches for an external resource. Whether the
material supports a conclusion is the model's judgment; the ledger guarantees
existence, version and true read state.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re

LAYER_REQUESTED = "requested"
LAYER_SEARCH_RESULT = "search_result"
LAYER_OBSERVED = "observed"
LAYER_HISTORICAL = "historical"
LAYER_FAILED = "failed"
LAYER_SUPERSEDED = "superseded"

CITABLE = {"observed", "historical"}
MEMORY_LOCATOR = re.compile(r"^memory://([A-Za-z0-9_.:-]{1,200})$")
# `citation_key`: an http(s) locator as scheme, authority, path with query (the #fragment is never
# sent), and in the last the characters RFC 3986 respells. A backslash stays: browsers read a slash.
WEB_LOCATOR = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*)://([^/?#]*)([^#]*)", re.S)
HOST_PORT = re.compile(r"(\[[^\]]*\]|[^:]*)(?::(.*))?", re.S)
UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
RESPELLED = re.compile(r"%[0-9A-Fa-f]{2}|[^A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%\\]")
DEFAULT_PORTS = {"http": "80", "https": "443"}
ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
EVIDENCE_ID_SHAPE = re.compile(r"(?:web|computer|mem)_[0-9a-f]*|[0-9a-f]{64}")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SOURCE_RECEIPT_FORMAT = "kin-source-receipt-v1"
SOURCE_RECEIPT_V2 = "kin-source-receipt-v2"
COMPUTER_ADAPTERS = {"kin-computer-reader-v1", "kin-computer-use-v1"}
WEB_RECEIPT_V2 = "kin-web-receipt-v2"


def web_delivery(receipt):
    """Content version and tool-delivered ranges; not a claim of comprehension."""
    if receipt.get("receipt_format") != WEB_RECEIPT_V2:
        return None
    return {key: copy.deepcopy(receipt.get(key)) for key in (
        "receipt_format", "content_sha256", "content_chars", "raw_body_sha256",
        "raw_body_bytes", "body_bytes_representation", "raw_body_complete",
        "delivered_ranges", "semantic_classification",
    )}


def valid_web_delivery(delivery, version):
    if not isinstance(delivery, dict) or delivery.get("receipt_format") != WEB_RECEIPT_V2:
        return False
    if delivery.get("content_sha256") != version or not SHA256.fullmatch(str(version or "")):
        return False
    if delivery.get("semantic_classification") != "model-required":
        return False
    if (delivery.get("body_bytes_representation") != "decoded-http-entity"
            or delivery.get("raw_body_complete") is not True):
        return False
    length = delivery.get("content_chars")
    raw_bytes = delivery.get("raw_body_bytes")
    if (type(length) is not int or length <= 0 or type(raw_bytes) is not int or raw_bytes <= 0
            or not SHA256.fullmatch(str(delivery.get("raw_body_sha256") or ""))):
        return False
    ranges = delivery.get("delivered_ranges")
    if not isinstance(ranges, list) or not ranges:
        return False
    for page in ranges:
        if not isinstance(page, dict):
            return False
        start, end = page.get("start"), page.get("end")
        if (type(start) is not int or type(end) is not int or not 0 <= start < end <= length
                or not SHA256.fullmatch(str(page.get("sha256") or ""))
                or not isinstance(page.get("delivered_at"), str) or not page["delivered_at"]):
            return False
    return True


def _entry(state, locator, *, evidence_id=None, version=None, title="", basis="", recorded_at=None,
           execution_id=None, attempt=None, tool=None, adapter=None, delivery=None):
    return {"state": state, "locator": locator, "evidence_id": evidence_id,
            "version": version, "title": title, "basis": basis, "recorded_at": recorded_at,
            "execution_id": execution_id, "attempt": attempt, "tool": tool, "adapter": adapter,
            **({"delivery": copy.deepcopy(delivery)} if delivery is not None else {})}


def _seal_payload(entry, execution_id, attempt, receipt_format=None):
    return {
        "receipt_format": receipt_format or (SOURCE_RECEIPT_V2 if "delivery" in entry else SOURCE_RECEIPT_FORMAT),
        "verified_by_execution": str(execution_id),
        "verified_by_attempt": int(attempt),
        **{key: entry.get(key) for key in (
            "state", "locator", "evidence_id", "version", "title", "basis", "recorded_at",
            "execution_id", "attempt", "tool", "adapter",
        )},
        # Omit this extension for old receipts: their original digest stays valid.
        **({"delivery": copy.deepcopy(entry["delivery"])} if "delivery" in entry else {}),
    }


def seal_source_receipt(entry, *, execution_id, attempt):
    """Seal a receipt after host verification.

    This is an integrity envelope, not a cryptographic signature: its authority
    comes from being added by the host after strict Findings validation. Model
    output cannot add fields to Citation (pydantic forbids extras).
    """
    payload = _seal_payload(entry, execution_id, attempt)
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return {**payload, "receipt_digest": hashlib.sha256(canonical.encode()).hexdigest()}


def valid_source_receipt(receipt, *, execution_id=None, attempt=None):
    if not isinstance(receipt, dict) or receipt.get("receipt_format") not in {SOURCE_RECEIPT_FORMAT, SOURCE_RECEIPT_V2}:
        return False
    if receipt["receipt_format"] == SOURCE_RECEIPT_V2 and "delivery" not in receipt:
        return False
    if receipt.get("state") not in CITABLE or not receipt.get("locator"):
        return False
    if (not receipt.get("evidence_id") or receipt.get("version") is None
            or receipt.get("version") == ""):
        return False
    if "delivery" in receipt and not valid_web_delivery(receipt["delivery"], receipt["version"]):
        return False
    try:
        verified_attempt = int(receipt.get("verified_by_attempt"))
    except (TypeError, ValueError):
        return False
    verified_execution = str(receipt.get("verified_by_execution") or "")
    if not verified_execution or verified_attempt < 1:
        return False
    if execution_id is not None and verified_execution != str(execution_id):
        return False
    if attempt is not None and verified_attempt != int(attempt):
        return False
    payload = _seal_payload(receipt, verified_execution, verified_attempt, receipt["receipt_format"])
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return receipt.get("receipt_digest") == hashlib.sha256(canonical.encode()).hexdigest()


def valid_web_receipt(receipt, *, execution_id, attempt):
    if not isinstance(receipt, dict) or receipt.get("execution_id") != str(execution_id):
        return False
    if receipt.get("attempt") != int(attempt) or receipt.get("state") not in {
        LAYER_SEARCH_RESULT, LAYER_OBSERVED, LAYER_FAILED,
    }:
        return False
    if not re.fullmatch(r"web_[0-9a-f]{32}", str(receipt.get("evidence_id") or "")):
        return False
    if receipt.get("tool") not in {"web_search", "read_page"} or not receipt.get("locator"):
        return False
    if receipt.get("state") == LAYER_OBSERVED:
        if "receipt_format" in receipt and (
            receipt["receipt_format"] != WEB_RECEIPT_V2
            or not valid_web_delivery(web_delivery(receipt), receipt.get("version"))
        ):
            return False
        return receipt.get("tool") == "read_page" and bool(
            SHA256.fullmatch(str(receipt.get("version") or "")) and receipt.get("read_at")
        )
    return True


def valid_computer_receipt(receipt, *, execution_id, attempt):
    """Only a successful receipt written by a current host adapter is evidence."""
    if not isinstance(receipt, dict) or receipt.get("state") != LAYER_OBSERVED:
        return False
    if receipt.get("execution_id") != str(execution_id) or receipt.get("attempt") != int(attempt):
        return False
    if receipt.get("adapter") not in COMPUTER_ADAPTERS:
        return False
    if receipt.get("tool") not in {
        "read_computer_context", "read_computer_resource", "open_browser_page",
        "read_browser_page", "navigate_browser_page", "click_browser_element",
        "type_browser_text", "observe_native_app", "click_native_element",
        "scroll_native_app",
    }:
        return False
    evidence_id = receipt.get("evidence_id")
    return bool(
        re.fullmatch(r"computer_[0-9a-f]{32}", str(evidence_id or ""))
        and SHA256.fullmatch(str(receipt.get("version") or ""))
        and receipt.get("locator")
        and receipt.get("observed_at")
    )


def build_ledger(topic, *, web_observations=(), computer_observations=(), continuation=None,
                 execution_id=None, attempt=None):
    """The run's citable universe plus the known non-citable states."""
    entries = []

    for item in topic.get("known_evidence", []):
        # Host-supplied material: citable as its memory:// locator, at the revision
        # the host supplied. A URL inside its text is not a source — it is text.
        if isinstance(item, dict) and item.get("source_id"):
            entries.append(_entry(LAYER_HISTORICAL, "memory://" + str(item["source_id"]),
                                  evidence_id=item.get("id"), version=item.get("revision"),
                                  basis="supplied-evidence"))
    for previous in topic.get("previous_explorations", []):
        # A URL in an old result is not proof. Only a source receipt sealed by the
        # host after that exact completed exploration may become historical.
        if not isinstance(previous, dict) or previous.get("state") != "complete":
            continue
        result = previous.get("result") or {}
        for source in result.get("sources", []):
            receipt = source.get("receipt") if isinstance(source, dict) else None
            if (not isinstance(source, dict) or not source.get("url")
                    or not valid_source_receipt(receipt, execution_id=previous.get("id"))
                    or not _named_by([receipt.get("locator")])(source.get("url"))):
                continue
            entries.append(_entry(
                LAYER_HISTORICAL, source["url"], evidence_id=receipt.get("evidence_id"),
                version=receipt.get("version"), title=source.get("title", ""),
                basis="exploration:" + str(previous.get("id")),
                recorded_at=receipt.get("recorded_at") or previous.get("created_at"),
                execution_id=receipt.get("execution_id"), attempt=receipt.get("attempt"),
                tool=receipt.get("tool"), adapter=receipt.get("adapter"),
                delivery=receipt.get("delivery"),
            ))
    for receipt in web_observations or []:
        if execution_id is None or attempt is None or not valid_web_receipt(
                receipt, execution_id=execution_id, attempt=attempt):
            continue
        state = receipt.get("state") if receipt.get("state") in {
            LAYER_SEARCH_RESULT, LAYER_OBSERVED, LAYER_FAILED} else LAYER_FAILED
        entries.append(_entry(state, receipt["locator"], evidence_id=receipt.get("evidence_id"),
                              version=receipt.get("version"), title=receipt.get("title", ""),
                              basis=receipt.get("tool", "web"), recorded_at=receipt.get("read_at"),
                              execution_id=receipt.get("execution_id"), attempt=receipt.get("attempt"),
                              tool=receipt.get("tool"), adapter="kin-web-reader-v1",
                              delivery=web_delivery(receipt)))
        if state == LAYER_OBSERVED and receipt.get("requested_locator") != receipt["locator"]:
            # A redirect's requested address and final address are both this read.
            entries.append(_entry(LAYER_OBSERVED, receipt["requested_locator"],
                                  evidence_id=receipt.get("evidence_id"), version=receipt.get("version"),
                                  title=receipt.get("title", ""), basis="web-redirect",
                                  recorded_at=receipt.get("read_at"),
                                  execution_id=receipt.get("execution_id"), attempt=receipt.get("attempt"),
                                  tool=receipt.get("tool"), adapter="kin-web-reader-v1",
                                  delivery=web_delivery(receipt)))
    for observation in computer_observations or []:
        if (execution_id is not None and attempt is not None
                and valid_computer_receipt(observation, execution_id=execution_id, attempt=attempt)):
            entries.append(_entry(LAYER_OBSERVED, observation["locator"],
                                  evidence_id=observation.get("evidence_id"), version=observation.get("version"),
                                  title=observation.get("title", ""), basis="computer",
                                  recorded_at=observation.get("observed_at"),
                                  execution_id=observation.get("execution_id"),
                                  attempt=observation.get("attempt"), tool=observation.get("tool"),
                                  adapter=observation.get("adapter")))
    validated_carried, _ = validate_continuation_sources(continuation, topic)
    for carried in validated_carried:
        # A previous attempt's legitimate receipts continue as historical — shape-
        # checked here, re-checked against the fresh ledger by callers.
        if isinstance(carried, dict):
            entries.append(_entry(LAYER_HISTORICAL, carried["locator"],
                                  evidence_id=carried.get("evidence_id"), version=carried.get("version"),
                                  title=carried.get("title", ""),
                                  basis="continuation:" + str((continuation or {}).get("exploration_id")),
                                  recorded_at=carried.get("recorded_at"),
                                  delivery=carried.get("delivery")))
    return entries


def validate_continuation_sources(continuation, topic):
    """Re-verify a checkpoint's carried sources before the new attempt runs:
    structurally sound, and memory:// receipts still match the evidence revision
    the host supplies now. A corrected evidence source supersedes the old one."""
    carried = (continuation or {}).get("sources_used", [])
    current = {str(item.get("source_id")): item.get("revision")
               for item in topic.get("known_evidence", [])
               if isinstance(item, dict) and item.get("source_id") and item.get("revision") is not None}
    valid, dropped = [], []
    for entry in carried:
        if not isinstance(entry, dict) or not entry.get("locator") or entry.get("state") not in CITABLE:
            dropped.append({"locator": (entry or {}).get("locator") if isinstance(entry, dict) else None,
                            "reason": "receipt-shape-invalid"})
            continue
        memory = MEMORY_LOCATOR.match(str(entry["locator"]))
        if memory:
            source_id = memory.group(1)
            if entry.get("version") is None or source_id not in current or current[source_id] != entry.get("version"):
                dropped.append({"locator": entry["locator"], "reason": "superseded-or-absent"})
                continue
        elif not valid_source_receipt(
                entry, execution_id=(continuation or {}).get("exploration_id"),
                attempt=(continuation or {}).get("attempt")):
            dropped.append({"locator": entry["locator"], "reason": "host-receipt-missing-or-invalid"})
            continue
        valid.append(entry)
    return valid, dropped


def _respelled(match):
    text = match.group()
    if len(text) == 3:
        char = chr(int(text[1:], 16))
        return char if char in UNRESERVED else text.upper()
    return "".join("%{:02X}".format(byte) for byte in text.encode("utf-8", "surrogatepass"))


def citation_key(locator):
    """What two spellings of one http(s) request share, or None for any other locator (memory://
    and the rest are compared as written). Scheme and host in lower case, no default port, no
    #fragment; in the path and query a non-ASCII or otherwise unsafe character and a %xx are one
    uppercase %XX, an unreserved character and its %XX are one (RFC 3986). A reserved character
    is never decoded, the query keeps its content and order, a trailing slash and http/https stay
    as written."""
    match = WEB_LOCATOR.match(locator) if isinstance(locator, str) else None
    scheme = match and match.group(1).lower()
    if scheme not in DEFAULT_PORTS:
        return None
    userinfo, at, host_port = match.group(2).rpartition("@")
    host, port = HOST_PORT.fullmatch(host_port).groups()
    if not host:
        return None
    port = "" if port is None or port == DEFAULT_PORTS[scheme] else ":" + port
    return (scheme + "://" + userinfo + at + host.translate(ASCII_LOWER) + port
            + RESPELLED.sub(_respelled, match.group(3)))


def _named_by(locators):
    """Whether a value names one of `locators`: as written, or an http(s) one by `citation_key`."""
    exact = {value for value in locators if isinstance(value, str)}
    keys = {citation_key(value) for value in exact} - {None}
    return lambda value: value in exact or (bool(keys) and citation_key(value) in keys)


def legitimize(ledger, url):
    """The receipt for a citation, or None. Exact first; then, among citable receipts only, another
    spelling of the same http(s) request (`citation_key`). No prefix matching."""
    if url == "computer://current-context":
        for entry in reversed(ledger):
            if entry["state"] == LAYER_OBSERVED and entry["basis"] == "computer" and entry["locator"] == url:
                return entry
        return None
    memory = MEMORY_LOCATOR.match(str(url))
    if memory:
        for entry in reversed(ledger):
            if entry["state"] == LAYER_HISTORICAL and entry["locator"] == url:
                return entry
        return None
    # What this run read itself comes before a receipt carried in from a checkpoint or an earlier
    # exploration: a page read again now is the version the result was written from (K4-17).
    matches = [entry for entry in ledger if entry["state"] in CITABLE and entry["locator"] == url]
    key = None if matches else citation_key(url)
    if key is not None:
        matches = [entry for entry in ledger
                   if entry["state"] in CITABLE and citation_key(entry["locator"]) == key]
    observed = [entry for entry in matches if entry["state"] == LAYER_OBSERVED]
    return (observed or matches or [None])[-1]


def _backed(ledger):
    """Whether an evidence_map id names a citable receipt: its full evidence_id, or its locator."""
    citable = [entry for entry in ledger if entry["state"] in CITABLE]
    ids = {entry["evidence_id"] for entry in citable if entry.get("evidence_id")}
    named = _named_by(entry["locator"] for entry in citable)
    return lambda identifier: identifier in ids or named(identifier)


def verify_citations(findings, ledger):
    """(rejected citations, rejected evidence ids). A citation must map to a citable
    receipt (`legitimize`); an evidence_map id must exist with a citable state."""
    rejected = [citation.url for citation in findings.sources
                if legitimize(ledger, citation.url) is None]
    unknown_ids = []
    if findings.evidence_map:
        backed = _backed(ledger)
        for claim, ids in findings.evidence_map.items():
            if not isinstance(ids, list) or not ids:
                unknown_ids.append(claim)
                continue
            unknown_ids.extend(str(identifier) for identifier in ids if not backed(identifier))
    return rejected, sorted(set(unknown_ids))


def rejection_counts(findings, ledger, web_observations=()):
    """Why each refused citation and evidence_map id was refused, as counts by class: no locator,
    title or text, so the counts outlast the words a settled run's files lose (workdirs). A locator
    is `memory-unknown`, `failed-read` (a read of it failed), `search-only` (a search result),
    `linked-only` (a link in a page read) or `unread`; an id that is no locator is `failed-read` or
    `search-only` by its receipt, else `id-shape` (a cut or miscopied id, a version hash) or
    `not-evidence`; a claim mapped to nothing is `empty-mapping`."""
    receipts = [receipt for receipt in web_observations or [] if isinstance(receipt, dict)]
    reads = [receipt for receipt in receipts if receipt.get("tool") == "read_page"]
    failed = _named_by([value for receipt in reads if receipt.get("state") == LAYER_FAILED
                        for value in (receipt.get("locator"), receipt.get("requested_locator"))])
    searched = _named_by([result.get("url") for receipt in receipts
                          if receipt.get("state") == LAYER_SEARCH_RESULT
                          for result in receipt.get("results") or [] if isinstance(result, dict)])
    linked = _named_by([link for receipt in reads if receipt.get("state") == LAYER_OBSERVED
                        for link in receipt.get("links") or []])
    failed_ids = {receipt.get("evidence_id") for receipt in reads if receipt.get("state") == LAYER_FAILED}
    search_ids = {receipt.get("evidence_id") for receipt in receipts if receipt.get("tool") == "web_search"}

    def locator_class(value):
        if str(value).startswith("memory://"):
            return "memory-unknown"
        return ("failed-read" if failed(value) else "search-only" if searched(value)
                else "linked-only" if linked(value) else "unread")

    def id_class(value):
        if "://" in value:
            return locator_class(value)
        if value in failed_ids:
            return "failed-read"
        if value in search_ids:
            return "search-only"
        return "id-shape" if EVIDENCE_ID_SHAPE.fullmatch(value) else "not-evidence"

    counts = {"sources": {}, "evidence_map": {}}

    def count(part, name):
        counts[part][name] = counts[part].get(name, 0) + 1

    for citation in findings.sources:
        if legitimize(ledger, citation.url) is None:
            count("sources", locator_class(citation.url))
    backed = _backed(ledger)
    refused = set()
    for ids in (findings.evidence_map or {}).values():
        if not isinstance(ids, list) or not ids:
            count("evidence_map", "empty-mapping")
            continue
        refused.update(str(identifier) for identifier in ids if not backed(identifier))
    for identifier in sorted(refused):
        count("evidence_map", id_class(identifier))
    return counts


def verified_sources(findings, ledger, *, execution_id=None, attempt=None):
    """Per-source verification for checkpoints: only citable receipts, with state."""
    verified = []
    for citation in findings.sources:
        entry = legitimize(ledger, citation.url)
        if entry:
            value = {**entry, "cited_as": citation.url, "citation_title": citation.title}
            if execution_id is not None and attempt is not None:
                value = seal_source_receipt(value, execution_id=execution_id, attempt=attempt)
                value.update(cited_as=citation.url, citation_title=citation.title)
            verified.append(value)
    return verified


def coverage(findings, ledger):
    """Evidence-map coverage for the receipt: claims named, claims backed."""
    if not findings.evidence_map:
        return {"mapped_claims": 0, "covered_claims": 0}
    backed = _backed(ledger)
    covered = sum(1 for ids in findings.evidence_map.values()
                  if isinstance(ids, list) and ids and all(backed(identifier) for identifier in ids))
    return {"mapped_claims": len(findings.evidence_map), "covered_claims": covered}


def summary(ledger):
    counts = {}
    for entry in ledger:
        counts[entry["state"]] = counts.get(entry["state"], 0) + 1
    return counts
