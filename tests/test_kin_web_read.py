"""Web reader tools: local stub servers only, never real network."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from kin_mind.web_read import WebReader, create_server

DDG_HTML = """
<html><body>
<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage">Example Page</a>
<a class="result__snippet">A snippet about the page.</a>
<a class="result__a" href="https://example.org/direct">Direct Link</a>
<a class="result__snippet">Another snippet.</a>
</body></html>
"""

PAGE_HTML = """
<html><head><title>Probe Page</title><style>body{color:red}</style></head>
<body><h1>The ledger is green</h1><script>var x=1;</script>
<p>Readable content.</p><a href="/next">Next</a></body></html>
"""


@pytest.fixture
def stub():
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            if self.path.startswith("/redirect"):
                self.send_response(301)
                self.send_header("Location", "/page")
                self.end_headers()
            elif self.path.startswith("/search"):
                self._send("text/html", DDG_HTML)
            elif self.path.startswith("/page"):
                self._send("text/html", PAGE_HTML)
            elif self.path.startswith("/data.json"):
                self._send("application/json", '{"answer": 42}')
            elif self.path.startswith("/binary"):
                self._send("application/pdf", b"%PDF-fake")
            else:
                self._send("text/plain", "plain text")

        def _send(self, content_type, body):
            body = body.encode() if isinstance(body, str) else body
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", hits
    server.shutdown()


def reader(tmp_path, **extra):
    return WebReader({"execution_id": "explore_test", "attempt": 1,
                      "ledger": str(tmp_path / "web-observations.json"),
                      "allow_hosts": ["127.0.0.1"], **extra})


def test_read_page_observed_receipt_and_redirect(tmp_path, stub):
    base, _hits = stub
    receipt = reader(tmp_path).read_page(base + "/redirect")
    assert receipt["state"] == "observed"
    assert receipt["requested_locator"] == base + "/redirect"
    assert receipt["locator"] == base + "/page"
    assert receipt["redirect_chain"] == [{"from": base + "/redirect", "to": base + "/page", "status": 301}]
    assert receipt["title"] == "Probe Page"
    assert "The ledger is green" in receipt["excerpt"] and "var x" not in receipt["excerpt"]
    assert receipt["version"] and receipt["evidence_id"].startswith("web_")
    assert receipt["execution_id"] == "explore_test" and receipt["attempt"] == 1
    stored = json.loads((tmp_path / "web-observations.json").read_text())
    assert list(stored) == [receipt["evidence_id"]]


def test_read_page_gates_content_type_and_size(tmp_path, stub):
    base, _ = stub
    pdf = reader(tmp_path).read_page(base + "/binary")
    assert pdf["state"] == "failed" and pdf["failure_reason"].startswith("unsupported-content-type")
    data = reader(tmp_path).read_page(base + "/data.json")
    assert data["state"] == "observed" and "42" in data["excerpt"]
    plain = reader(tmp_path).read_page(base + "/plain")
    assert plain["state"] == "observed"


def test_ssrf_and_unreachable_hosts_fail_closed(tmp_path):
    private = reader(tmp_path).read_page("http://192.168.1.1/admin")
    assert private["state"] == "failed" and private["failure_reason"] == "address-not-public"
    loopback = reader(tmp_path).read_page("http://localhost:9/x")
    assert loopback["state"] == "failed" and loopback["failure_reason"] == "address-not-public"
    absent = reader(tmp_path).read_page("http://nonexistent.invalid/page")
    # Fail closed; the exact refusal depends on the network's DNS behavior.
    assert absent["state"] == "failed" and absent["failure_reason"] in {
        "host-unresolvable", "fetch-failed", "address-not-public"}
    bad_scheme = reader(tmp_path).read_page("file:///etc/passwd")
    assert bad_scheme["state"] == "failed" and bad_scheme["failure_reason"] == "unsupported-url-scheme"
    credentialed = reader(tmp_path).read_page("http://user:pw@127.0.0.1/page")
    assert credentialed["state"] == "failed" and credentialed["failure_reason"] == "credentials-in-url-refused"


def test_search_results_are_visibility_evidence_only(tmp_path, stub):
    base, _ = stub
    receipt = reader(tmp_path, search_endpoint=base + "/search").search("synthetic query")
    assert receipt["state"] == "search_result"
    assert [r["url"] for r in receipt["results"]] == ["https://example.com/page", "https://example.org/direct"]
    assert receipt["results"][0]["title"] == "Example Page"
    assert "visibility" in receipt["note"]
    # A failed endpoint is a failed receipt, never an empty success.
    failed = reader(tmp_path, search_endpoint="http://192.168.1.1/search").search("q")
    assert failed["state"] == "failed"


def test_per_host_courtesy_limit(tmp_path, stub):
    base, _ = stub
    web = reader(tmp_path)
    for _ in range(4):
        assert web.read_page(base + "/page")["state"] == "observed"
    assert web.read_page(base + "/page")["failure_reason"] == "host-courtesy-limit"


def test_mcp_server_exposes_the_two_read_only_tools(tmp_path):
    server = create_server({"execution_id": "explore_test", "attempt": 1,
                            "ledger": str(tmp_path / "ledger.json")})
    tools = server._tool_manager.list_tools()
    assert {t.name for t in tools} == {"web_search", "read_page"}
