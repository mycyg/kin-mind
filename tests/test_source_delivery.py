"""Pagination provenance survives current, historical and continued citations."""
import copy
import hashlib
import json

import pytest

from kin_mind.source_ledger import (
    build_ledger,
    seal_source_receipt,
    valid_source_receipt,
    valid_web_receipt,
)


def receipt():
    text = "a real page with more content"
    sha = lambda value: hashlib.sha256(value.encode()).hexdigest()
    return {"state": "observed", "execution_id": "run1", "attempt": 1,
            "evidence_id": "web_" + "a" * 32, "tool": "read_page",
            "locator": "https://example.com/article", "requested_locator": "https://example.com/article",
            "version": sha(text), "read_at": "2026-09-20T01:00:00Z",
            "receipt_format": "kin-web-receipt-v2", "content_sha256": sha(text),
            "content_chars": len(text), "raw_body_sha256": sha(text), "raw_body_bytes": len(text),
            "delivered_ranges": [{"start": 0, "end": 11, "sha256": sha(text[:11]),
                                  "delivered_at": "2026-09-20T01:00:00Z"}]}


def test_delivery_seal_survives_historical_and_continuation():
    current = receipt()
    ledger = build_ledger({}, web_observations=[current], execution_id="run1", attempt=1)
    sealed = seal_source_receipt(ledger[0], execution_id="run1", attempt=1)
    assert valid_source_receipt(sealed)
    historical = build_ledger({"previous_explorations": [{
        "id": "run1", "state": "complete", "result": {"sources": [{
            "url": current["locator"], "receipt": sealed,
        }]},
    }]})
    assert historical[0]["state"] == "historical"
    assert historical[0]["delivery"] == sealed["delivery"]
    continued = build_ledger({}, continuation={"exploration_id": "run1", "attempt": 1,
                                               "sources_used": [sealed]})
    assert continued[0]["delivery"] == sealed["delivery"]
    altered = copy.deepcopy(sealed)
    altered["delivery"]["delivered_ranges"][0]["end"] += 1
    assert not valid_source_receipt(altered)


@pytest.mark.parametrize("change", [
    {"content_sha256": "b" * 64}, {"content_chars": 0}, {"content_chars": True},
    {"delivered_ranges": []}, {"raw_body_bytes": 0}, {"raw_body_sha256": "invalid"},
    {"receipt_format": "unknown"},
])
def test_malformed_new_receipt_not_citable(change):
    current = {**receipt(), **change}
    assert not valid_web_receipt(current, execution_id="run1", attempt=1)
    assert not build_ledger({}, web_observations=[current], execution_id="run1", attempt=1)


def test_range_outside_page_or_wrong_execution_not_citable():
    current = receipt()
    assert valid_web_receipt(current, execution_id="run1", attempt=1)
    assert not valid_web_receipt(current, execution_id="run2", attempt=1)
    current["delivered_ranges"][0]["end"] = 10000
    assert not valid_web_receipt(current, execution_id="run1", attempt=1)


def test_legacy_seal_digest_unchanged():
    legacy = {"state": "observed", "locator": "https://example.com/old", "evidence_id": "old",
              "version": "old-version", "title": "", "basis": "web", "recorded_at": "then",
              "execution_id": "old", "attempt": 1, "tool": "read_page", "adapter": "kin-web-reader-v1"}
    old_payload = {"receipt_format": "kin-source-receipt-v1", "verified_by_execution": "old",
                   "verified_by_attempt": 1, **legacy}
    old_digest = hashlib.sha256(json.dumps(old_payload, sort_keys=True, ensure_ascii=False,
                                          separators=(",", ":")).encode()).hexdigest()
    sealed = seal_source_receipt(legacy, execution_id="old", attempt=1)
    assert sealed["receipt_digest"] == old_digest
    assert "delivery" not in sealed and valid_source_receipt(sealed)
