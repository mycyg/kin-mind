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

import re

LAYER_REQUESTED = "requested"
LAYER_SEARCH_RESULT = "search_result"
LAYER_OBSERVED = "observed"
LAYER_HISTORICAL = "historical"
LAYER_FAILED = "failed"
LAYER_SUPERSEDED = "superseded"

CITABLE = {"observed", "historical"}
MEMORY_LOCATOR = re.compile(r"^memory://([A-Za-z0-9_.:-]{1,200})$")


def _entry(state, locator, *, evidence_id=None, version=None, title="", basis="", recorded_at=None):
    return {"state": state, "locator": locator, "evidence_id": evidence_id,
            "version": version, "title": title, "basis": basis, "recorded_at": recorded_at}


def build_ledger(topic, *, web_observations=(), computer_observations=(), continuation=None):
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
        # Prior verified exploration results stay citable with their time nature:
        # recorded then, not re-verified now.
        if not isinstance(previous, dict):
            continue
        result = previous.get("result") or {}
        for source in result.get("sources", []):
            if isinstance(source, dict) and source.get("url"):
                entries.append(_entry(LAYER_HISTORICAL, source["url"], title=source.get("title", ""),
                                      basis="exploration:" + str(previous.get("id")),
                                      recorded_at=previous.get("created_at")))
    for receipt in web_observations or []:
        if not isinstance(receipt, dict) or not receipt.get("locator"):
            continue
        state = receipt.get("state") if receipt.get("state") in {
            LAYER_SEARCH_RESULT, LAYER_OBSERVED, LAYER_FAILED} else LAYER_FAILED
        entries.append(_entry(state, receipt["locator"], evidence_id=receipt.get("evidence_id"),
                              version=receipt.get("version"), title=receipt.get("title", ""),
                              basis=receipt.get("tool", "web"), recorded_at=receipt.get("read_at")))
        if state == LAYER_OBSERVED and receipt.get("requested_locator") != receipt["locator"]:
            # A redirect's requested address and final address are both this read.
            entries.append(_entry(LAYER_OBSERVED, receipt["requested_locator"],
                                  evidence_id=receipt.get("evidence_id"), version=receipt.get("version"),
                                  title=receipt.get("title", ""), basis="web-redirect",
                                  recorded_at=receipt.get("read_at")))
    for observation in computer_observations or []:
        if isinstance(observation, dict) and observation.get("locator"):
            entries.append(_entry(LAYER_OBSERVED, observation["locator"],
                                  evidence_id=observation.get("id"), version=observation.get("version"),
                                  title=observation.get("title", ""), basis="computer",
                                  recorded_at=observation.get("observed_at")))
    for carried in (continuation or {}).get("sources_used", []):
        # A previous attempt's legitimate receipts continue as historical — shape-
        # checked here, re-checked against the fresh ledger by callers.
        if isinstance(carried, dict) and carried.get("locator") and carried.get("state") in CITABLE:
            entries.append(_entry(LAYER_HISTORICAL, carried["locator"],
                                  evidence_id=carried.get("evidence_id"), version=carried.get("version"),
                                  title=carried.get("title", ""),
                                  basis="continuation:" + str((continuation or {}).get("exploration_id")),
                                  recorded_at=carried.get("recorded_at")))
    return entries


def validate_continuation_sources(continuation, topic):
    """Re-verify a checkpoint's carried sources before the new attempt runs:
    structurally sound, and memory:// receipts still match the evidence revision
    the host supplies now. A corrected evidence source supersedes the old one."""
    carried = (continuation or {}).get("sources_used", [])
    current = {str(item.get("source_id")): item.get("revision")
               for item in topic.get("known_evidence", []) if isinstance(item, dict)}
    valid, dropped = [], []
    for entry in carried:
        if not isinstance(entry, dict) or not entry.get("locator") or entry.get("state") not in CITABLE:
            dropped.append({"locator": (entry or {}).get("locator") if isinstance(entry, dict) else None,
                            "reason": "receipt-shape-invalid"})
            continue
        memory = MEMORY_LOCATOR.match(str(entry["locator"]))
        if memory:
            source_id = memory.group(1)
            if current.get(source_id) != entry.get("version"):
                dropped.append({"locator": entry["locator"], "reason": "superseded-or-absent"})
                continue
        valid.append(entry)
    return valid, dropped


def legitimize(ledger, url):
    """The exact-match receipt for a citation, or None. No prefix matching."""
    if url == "computer://current-context":
        for entry in ledger:
            if entry["state"] == LAYER_OBSERVED and entry["basis"] == "computer" and entry["locator"] == url:
                return entry
        return None
    memory = MEMORY_LOCATOR.match(str(url))
    if memory:
        for entry in ledger:
            if entry["state"] == LAYER_HISTORICAL and entry["locator"] == url:
                return entry
        return None
    for entry in ledger:
        if entry["state"] in CITABLE and entry["locator"] == url:
            return entry
    return None


def verify_citations(findings, ledger):
    """(rejected citations, rejected evidence ids). A citation must map exactly to
    a citable receipt; an evidence_map id must exist with a citable state."""
    rejected = [citation.url for citation in findings.sources
                if legitimize(ledger, citation.url) is None]
    unknown_ids = []
    if findings.evidence_map:
        known = {entry["evidence_id"] for entry in ledger
                 if entry["state"] in CITABLE and entry.get("evidence_id")}
        known |= {entry["locator"] for entry in ledger if entry["state"] in CITABLE}
        for claim, ids in findings.evidence_map.items():
            if not isinstance(ids, list) or not ids:
                unknown_ids.append(claim)
                continue
            unknown_ids.extend(str(identifier) for identifier in ids if identifier not in known)
    return rejected, sorted(set(unknown_ids))


def verified_sources(findings, ledger):
    """Per-source verification for checkpoints: only citable receipts, with state."""
    verified = []
    for citation in findings.sources:
        entry = legitimize(ledger, citation.url)
        if entry:
            verified.append({**entry, "cited_as": citation.url, "citation_title": citation.title})
    return verified


def coverage(findings, ledger):
    """Evidence-map coverage for the receipt: claims named, claims backed."""
    if not findings.evidence_map:
        return {"mapped_claims": 0, "covered_claims": 0}
    known = {entry["evidence_id"] for entry in ledger
             if entry["state"] in CITABLE and entry.get("evidence_id")}
    known |= {entry["locator"] for entry in ledger if entry["state"] in CITABLE}
    covered = sum(1 for ids in findings.evidence_map.values()
                  if isinstance(ids, list) and ids and all(identifier in known for identifier in ids))
    return {"mapped_claims": len(findings.evidence_map), "covered_claims": covered}


def summary(ledger):
    counts = {}
    for entry in ledger:
        counts[entry["state"]] = counts.get(entry["state"], 0) + 1
    return counts
