"""Host-only verification of replacement deliveries; never invent a receipt."""
from __future__ import annotations

import hashlib
import io
import json
import re
import sys
import zipfile
from pathlib import Path


def verify_replacement(directory, proposal):
    directory = Path(directory)
    def read(identifier):
        if not re.fullmatch(r"[\w-]{1,120}", identifier):
            raise ValueError("Invalid outbox id")
        return json.loads((directory / (identifier + ".json")).read_text())
    def content(record):
        artifact = record["artifact"]
        raw = Path(artifact["path"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != artifact["sha256"]:
            raise ValueError("Artifact changed")
        return raw
    attempt = read(proposal["attemptId"])
    # For legacy attempts, the migration separately proves the SDK upload
    # error precedes send. Its exact immutable record hash is required.
    if not (attempt.get("stage") == "upload-failed" and attempt.get("submissionStarted") is False):
        original = hashlib.sha256((directory / (proposal["attemptId"] + ".json")).read_bytes()).hexdigest()
        if not (proposal.get("legacyUploadRecordSha256") == original and
                attempt.get("reason") in {"file upload failed", "image upload failed"}):
            raise ValueError("Submission boundary not proven")
    targets = [read(identifier) for identifier in proposal["replacementIds"]]
    if not targets or any(r.get("state") != "accepted" or not r.get("messageId") for r in targets):
        raise ValueError("Replacement not accepted")
    replacement = b"".join(content(r) for r in targets)
    expected = content(attempt)
    method = "identical-bytes"
    if replacement != expected:
        # A ZIP may deliver the same original file. Read in memory without
        # extracting paths or executing archive contents.
        found = False
        with zipfile.ZipFile(io.BytesIO(replacement)) as archive:
            for entry in archive.infolist():
                if not entry.is_dir() and entry.file_size == len(expected) and archive.read(entry) == expected:
                    found = True
                    break
        if not found:
            raise ValueError("Replacement does not contain the original artifact")
        method = "archive-member-identical-bytes"
    return {"state": "not-submitted", "attemptId": attempt["id"], "attemptSha256": hashlib.sha256(expected).hexdigest(),
            "method": method, "fulfilledBy": [
                {"outboxId": r["id"], "messageId": r["messageId"], "sha256": r["artifact"]["sha256"], "verified": True}
                for r in targets]}


if __name__ == "__main__":
    request = json.load(sys.stdin)
    print(json.dumps(verify_replacement(request["directory"], request["proposal"])))
