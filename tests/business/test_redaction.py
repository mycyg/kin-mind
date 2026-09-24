"""One set of redaction rules, applied where host tool events are stored (E1-06), and the
package without its removed `.memory` stack (E1-05)."""
import json
import subprocess
import sys

import pytest

from eventmem.core import Engine
from eventmem.core.hosts import handle

from kin_mind.computer import redact

SECRETS = [
    ('{"api_key": "abc123secret"}', "abc123secret"),
    ('"token": "tok-value-1"', "tok-value-1"),
    ("x-api-key: key-value-2", "key-value-2"),
    ("github_pat_11ABCDEFGHIJKLMNOPQRSTUV", "github_pat_11ABCDEFGHIJKLMNOPQRSTUV"),
    ("gho_ABCDEFGHIJKLMNOPQRSTU", "gho_ABCDEFGHIJKLMNOPQRSTU"),
    ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
    ("AIzaSyA1234567890abcdefghijklmnopqrstu", "AIzaSyA1234567890abcdefghijklmnopqrstu"),
    ("密码：hunter2", "hunter2"),
    ("api_key=plainvalue", "plainvalue"),
]


@pytest.mark.parametrize("text,secret", SECRETS)
def test_each_form_of_a_secret_is_taken_out(text, secret):
    assert secret not in redact(text)


def test_ordinary_text_is_left_alone():
    for text in ("The meeting is at 3pm; tokens used: 12", "我们周末去看海吧", "max_tokens=512"):
        assert redact(text) == text


def test_a_tool_event_is_stored_without_its_secrets(tmp_path):
    engine = Engine(tmp_path / "db")
    handle(engine, "tool", {"session": "s1", "tool_name": "shell", "tool_use_id": "t1",
                            "tool_input": {"command": "curl -H 'Authorization: Bearer abcdefghijklmnop' x"},
                            "tool_response": '{"token": "tok-value-1", "ok": true}',
                            "memory_context_managed": True})
    with engine.db.connect() as conn:
        stored = [json.loads(row[0]) for row in conn.execute("SELECT data FROM sources")]
    text = json.dumps(stored, ensure_ascii=False)
    assert "abcdefghijklmnop" not in text and "tok-value-1" not in text
    blobs = "".join(path.read_text(errors="ignore") for path in (tmp_path / "db" / "blobs").iterdir())
    assert "abcdefghijklmnop" not in blobs and "tok-value-1" not in blobs


def test_the_package_no_longer_loads_the_removed_stack():
    code = ("import sys, eventmem, eventmem.core, eventmem.cli; "
            "print(sorted(m for m in sys.modules if m in {'eventmem.store','eventmem.index','eventmem.recall',"
            "'eventmem.schema','eventmem.extract','eventmem.consolidate','eventmem.llm','eventmem.scrub','yaml'}))")
    found = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip()
    assert found == "[]"
