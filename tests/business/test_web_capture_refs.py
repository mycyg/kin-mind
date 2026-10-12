"""Web sources retain actual capture references without content-hash receipt gates."""
import json
import uuid

import pytest

from kin_mind.source_ledger import (build_ledger, seal_source_receipt, valid_source_receipt,
                                   valid_web_delivery, valid_web_receipt, web_delivery)
from kin_mind.web_read import WebReader


@pytest.fixture
def reader(tmp_path, monkeypatch):
    value = WebReader({"ledger": str(tmp_path / "web-observations.json"), "execution_id": "explore-current", "attempt": 1})
    def fetch(url, **kwargs):
        return {"body": b"First captured words. A later section.", "headers": {"content-type": "text/plain"},
                "status_code": 200, "body_over_limit": False, "url": url, "encoding": "utf-8"}, [], None
    monkeypatch.setattr(value, "_fetch", fetch)
    return value


def test_captures_have_independent_ids_and_record_actual_delivered_ranges(reader):
    one = reader.read_page("https://source.invalid/page", limit=8, tool_call_id="native-call-1")
    two = reader.read_page("https://source.invalid/page", limit=8, tool_call_id="native-call-2")
    assert one["evidence_id"] != two["evidence_id"]
    assert uuid.UUID(hex=one["evidence_id"][4:]).version == 4
    assert one["version"] == one["capture_id"] == one["evidence_id"]
    assert one["tool_call_id"] == "native-call-1"
    assert one["delivered_range"].keys() == {"start", "end", "delivered_at"}
    assert not any("sha256" in key or "digest" in key for key in one)
    assert valid_web_receipt(one, execution_id="explore-current", attempt=1)
    assert valid_web_delivery(web_delivery(one), one["version"])
    assert not valid_web_receipt(one, execution_id="another-run", attempt=1)
    assert not valid_web_receipt(one, execution_id="explore-current", attempt=2)


def test_continuation_uses_its_capture_reference_without_body_hash_or_length_rejection(reader):
    original = reader.read_page("https://source.invalid/page", limit=8)
    reader._content_path(original["evidence_id"]).write_text("Updated captured words and a different length.")
    continued = reader.read_page(original["locator"], evidence_id=original["evidence_id"],
                                 expected_version=original["version"], offset=8, limit=12)
    assert continued["state"] == "observed"
    assert continued["evidence_id"] == original["evidence_id"]
    assert continued["text"] == "Updated captured words and a different length."[8:20]
    assert continued["content_chars"] == len("Updated captured words and a different length.")
    assert valid_web_delivery(web_delivery(continued), continued["version"])
    wrong = reader.read_page(original["locator"], evidence_id=original["evidence_id"], expected_version="another-capture", offset=8)
    assert wrong["state"] == "failed" and wrong["failure_reason"] == "source-version-changed"
    foreign = reader.read_page("https://other.invalid/page", evidence_id=original["evidence_id"], expected_version=original["version"], offset=8)
    assert foreign["state"] == "failed" and foreign["failure_reason"] == "continuation-source-mismatch"


def test_legacy_capture_version_is_an_opaque_reference_and_old_hashes_are_not_carried(reader):
    original = reader.read_page("https://source.invalid/page", limit=8)
    rows = json.loads(reader.ledger.read_text())
    stored = rows[original["evidence_id"]]
    stored.pop("capture_id")
    stored.update(version="a" * 64, content_sha256="obsolete", raw_body_sha256="obsolete")
    stored["delivered_ranges"][0]["sha256"] = "obsolete"
    reader.ledger.write_text(json.dumps(rows))
    reader._content_path(original["evidence_id"]).write_text("Different legacy capture bytes; still its stored reference.")
    continued = reader.read_page(original["locator"], evidence_id=original["evidence_id"], expected_version="a" * 64, offset=8)
    assert continued["state"] == "observed"
    delivery = web_delivery(continued)
    assert valid_web_delivery(delivery, "a" * 64)
    assert "sha256" not in json.dumps(delivery)


def test_host_source_receipts_keep_execution_and_capture_facts_without_a_digest(reader):
    captured = reader.read_page("https://source.invalid/page")
    entry = build_ledger({}, web_observations=[captured], execution_id="explore-current", attempt=1)[0]
    receipt = seal_source_receipt(entry, execution_id="explore-current", attempt=1)
    assert "receipt_digest" not in receipt
    assert valid_source_receipt(receipt, execution_id="explore-current", attempt=1)
    assert valid_source_receipt({**receipt, "title": "Updated title"}, execution_id="explore-current", attempt=1)
    assert not valid_source_receipt(receipt, execution_id="another-run", attempt=1)
    assert not valid_source_receipt({**receipt, "state": "failed"}, execution_id="explore-current", attempt=1)
    assert not valid_source_receipt({**receipt, "evidence_id": None}, execution_id="explore-current", attempt=1)
