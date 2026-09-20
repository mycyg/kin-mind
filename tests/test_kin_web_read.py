"""Web reader tools: local stub servers only, never real network."""

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from kin_mind.web_read import MAX_BYTES, MAX_TEXT, WebReader, create_server

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
LONG_TEXT = "long-page:" + ("0123456789abcdef" * 2400)


@pytest.fixture
def stub():
    state = {"hits": [], "version": "one"}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["hits"].append(self.path)
            if self.path.startswith("/redirect"):
                self.send_response(301)
                self.send_header("Location", "/page")
                self.end_headers()
            elif self.path.startswith("/search-empty"):
                self._send(
                    "text/html",
                    "<html><title>Request review</title><body>Confirm this request to continue.</body></html>",
                )
            elif self.path.startswith("/search"):
                self._send("text/html", DDG_HTML)
            elif self.path.startswith("/page"):
                self._send("text/html", PAGE_HTML)
            elif self.path.startswith("/long"):
                self._send("text/html", "<html><body>" + LONG_TEXT + "</body></html>")
            elif self.path.startswith("/versioned"):
                self._send("text/plain", "version:" + state["version"])
            elif self.path.startswith("/empty"):
                self._send("text/html", "<html><body>   </body></html>")
            elif self.path.startswith("/challenge"):
                self._send("text/html", "<html><title>Just a moment</title><body>Verify you are human</body></html>")
            elif self.path.startswith("/status/404"):
                self._send("text/html", "<html><body>Not found</body></html>", status=404)
            elif self.path.startswith("/status/403"):
                self._send("text/html", "<html><body>Access denied</body></html>", status=403)
            elif self.path.startswith("/data.json"):
                self._send("application/json", '{"answer": 42}')
            elif self.path.startswith("/binary"):
                self._send("application/pdf", b"%PDF-fake")
            else:
                self._send("text/plain", "plain text")

        def _send(self, content_type, body, *, status=200):
            body = body.encode() if isinstance(body, str) else body
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", state
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
    assert receipt["results_empty"] is False
    assert "Example Page" in receipt["response_excerpt"]
    assert receipt["semantic_classification"] == "model-required"

    # HTTP 200 with no parsed results is still model-visible evidence. The
    # adapter does not classify the page with a challenge-word list.
    empty = reader(tmp_path / "empty", search_endpoint=base + "/search-empty").search("q")
    assert empty["state"] == "search_result" and empty["results"] == []
    assert empty["results_empty"] is True
    assert "Confirm this request" in empty["response_excerpt"]
    assert empty["semantic_classification"] == "model-required"
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


def test_long_page_freezes_full_content_and_continues_without_refetch(tmp_path, stub):
    base, state = stub
    web = reader(tmp_path)
    first = web.read_page(base + "/long", limit=4096)
    assert first["state"] == "observed" and first["receipt_format"] == "kin-web-receipt-v2"
    assert first["text"] == LONG_TEXT[:4096]
    assert {key: first["delivered_range"][key] for key in ("start", "end", "sha256")} == {
        "start": 0, "end": 4096, "sha256": hashlib.sha256(LONG_TEXT[:4096].encode()).hexdigest()}
    assert set(first["delivered_range"]) == {"start", "end", "sha256", "delivered_at"}
    assert first["delivered_range"]["delivered_at"]
    assert first["content_sha256"] == first["version"] == hashlib.sha256(LONG_TEXT.encode()).hexdigest()
    assert first["content_chars"] == len(LONG_TEXT)
    assert first["truncated"] is True and first["next_offset"] == 4096
    assert first["excerpt"] == LONG_TEXT[:2000]
    sidecar = tmp_path / first["content_ref"]
    assert sidecar.read_text() == LONG_TEXT and sidecar.stat().st_mode & 0o777 == 0o600

    second = web.read_page(
        first["locator"], offset=first["next_offset"], limit=4096,
        evidence_id=first["evidence_id"], expected_version=first["version"],
    )
    assert second["state"] == "observed" and second["evidence_id"] == first["evidence_id"]
    assert second["text"] == LONG_TEXT[4096:8192]
    assert state["hits"].count("/long") == 1

    # Re-delivering the exact same range is idempotent in the source envelope.
    again = web.read_page(
        first["locator"], offset=4096, limit=4096,
        evidence_id=first["evidence_id"], expected_version=first["version"],
    )
    assert again["state"] == "observed"
    stored = json.loads((tmp_path / "web-observations.json").read_text())[first["evidence_id"]]
    assert stored["delivered_ranges"] == [first["delivered_range"], second["delivered_range"]]


def test_page_continuation_is_execution_bound_and_integrity_checked(tmp_path, stub):
    base, _state = stub
    web = reader(tmp_path)
    first = web.read_page(base + "/long", limit=100)
    other = WebReader({"execution_id": "explore_other", "attempt": 1,
                       "ledger": str(tmp_path / "web-observations.json"),
                       "allow_hosts": ["127.0.0.1"]})
    cross = other.read_page(
        first["locator"], offset=100, limit=100, evidence_id=first["evidence_id"],
        expected_version=first["version"],
    )
    assert cross["state"] == "failed" and cross["failure_reason"] == "continuation-not-found"

    (tmp_path / first["content_ref"]).write_text("tampered")
    damaged = web.read_page(
        first["locator"], offset=100, limit=100, evidence_id=first["evidence_id"],
        expected_version=first["version"],
    )
    assert damaged["state"] == "failed" and damaged["failure_reason"] == "content-integrity-failed"


def test_page_continuation_refuses_a_symlinked_content_parent(tmp_path, stub):
    base, _state = stub
    web = reader(tmp_path)
    first = web.read_page(base + "/long", limit=100)
    content_dir = tmp_path / "web-content"
    moved_dir = tmp_path / "moved-web-content"
    content_dir.rename(moved_dir)
    content_dir.symlink_to(moved_dir, target_is_directory=True)

    refused = web.read_page(
        first["locator"], offset=100, limit=100, evidence_id=first["evidence_id"],
        expected_version=first["version"],
    )
    assert refused["state"] == "failed" and refused["failure_reason"] == "content-unavailable"


def test_delivery_ranges_are_deduplicated_and_bounded(tmp_path, stub):
    base, _state = stub
    web = reader(tmp_path)
    first = web.read_page(base + "/long", limit=1)
    for offset in range(1, 64):
        result = web.read_page(
            first["locator"], offset=offset, limit=1, evidence_id=first["evidence_id"],
            expected_version=first["version"],
        )
        assert result["state"] == "observed"
    duplicate = web.read_page(
        first["locator"], offset=1, limit=1, evidence_id=first["evidence_id"],
        expected_version=first["version"],
    )
    assert duplicate["state"] == "observed"
    blocked = web.read_page(
        first["locator"], offset=64, limit=1, evidence_id=first["evidence_id"],
        expected_version=first["version"],
    )
    assert blocked["state"] == "failed" and blocked["failure_reason"] == "delivery-range-limit"
    source = json.loads((tmp_path / "web-observations.json").read_text())[first["evidence_id"]]
    assert len(source["delivered_ranges"]) == 64


def test_http_errors_empty_pages_and_challenges_have_distinct_contracts(tmp_path, stub):
    base, _state = stub
    for status in (403, 404):
        failed = reader(tmp_path / str(status)).read_page(base + f"/status/{status}")
        assert failed["state"] == "failed"
        assert failed["failure_reason"] == f"http-status:{status}"
        assert failed["http_status"] == status and "content_ref" not in failed
    empty = reader(tmp_path / "empty").read_page(base + "/empty")
    assert empty["state"] == "failed" and empty["failure_reason"] == "empty-content"
    challenge = reader(tmp_path / "challenge").read_page(base + "/challenge")
    assert challenge["state"] == "observed"
    assert challenge["semantic_classification"] == "model-required"
    assert "Verify you are human" in challenge["text"] and "before treating it as an article" in challenge["semantic_note"]


def test_same_url_updates_get_distinct_full_text_versions(tmp_path, stub):
    base, state = stub
    first = reader(tmp_path).read_page(base + "/versioned")
    state["version"] = "two"
    second = reader(tmp_path).read_page(base + "/versioned")
    assert first["text"] == "version:one" and second["text"] == "version:two"
    assert first["version"] != second["version"] and first["evidence_id"] != second["evidence_id"]
    stale = reader(tmp_path / "stale").read_page(base + "/versioned", expected_version=first["version"])
    assert stale["state"] == "failed" and stale["failure_reason"] == "source-version-changed"
    assert stale["observed_version"] == second["version"]


def test_page_arguments_are_strict_and_do_not_start_network_reads(tmp_path, stub):
    base, state = stub
    web = reader(tmp_path)
    for kwargs, reason in [
        ({"limit": 0}, "invalid-page-limit"),
        ({"limit": MAX_TEXT + 1}, "invalid-page-limit"),
        ({"limit": True}, "invalid-page-limit"),
        ({"offset": True}, "invalid-page-offset"),
        ({"offset": 1}, "continuation-evidence-required"),
    ]:
        receipt = web.read_page(base + "/page", **kwargs)
        assert receipt["state"] == "failed" and receipt["failure_reason"] == reason
    assert state["hits"] == []


def test_streaming_limit_closes_after_the_first_byte_over_cap(tmp_path):
    observed = {"chunks": 0, "response_closed": False, "client_closed": False, "factory": None}

    class Response:
        def __init__(self):
            self.status_code = 200
            self.headers = {"content-type": "text/plain"}
            self.url = "https://public.example/large"
            self.encoding = "utf-8"
            self.extensions = {"kin_public_transport": {"hostname": "public.example",
                                                          "resolved_addresses": ("203.0.113.10",)}}
            self.is_redirect = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            observed["response_closed"] = True

        def iter_bytes(self, chunk_size=None):
            assert chunk_size == 64 * 1024
            for _ in range(20):
                observed["chunks"] += 1
                yield b"x" * (64 * 1024)

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            observed["client_closed"] = True

        def stream(self, method, url, headers):
            assert (method, url) == ("GET", "https://public.example/large") and headers["User-Agent"]
            return Response()

    def factory(**kwargs):
        observed["factory"] = kwargs
        return Client()

    web = WebReader({"execution_id": "explore_stream", "attempt": 1,
                     "ledger": str(tmp_path / "ledger.json")},
                    client_factory=factory, controlled_transport=True)
    receipt = web.read_page("https://public.example/large")
    assert receipt["state"] == "failed" and receipt["failure_reason"] == "response-over-512k"
    assert receipt["raw_body_bytes"] == MAX_BYTES + 1 and receipt["raw_body_complete"] is False
    assert receipt["body_bytes_representation"] == "decoded-http-entity"
    assert observed == {"chunks": 9, "response_closed": True, "client_closed": True,
                        "factory": {"timeout": 15, "follow_redirects": False}}


def test_mcp_delivery_excerpt_stays_inside_each_recorded_range(tmp_path, stub):
    base, _state = stub
    server = create_server({"execution_id": "explore_mcp", "attempt": 1,
                            "ledger": str(tmp_path / "ledger.json"),
                            "allow_hosts": ["127.0.0.1"],
                            "search_endpoint": base + "/search-empty"})
    read_page = server._tool_manager._tools["read_page"].fn
    web_search = server._tool_manager._tools["web_search"].fn

    first = read_page(base + "/long", limit=1000)
    assert first["text"] == first["excerpt"] == LONG_TEXT[:1000]
    assert first["delivered_range"]["start"] == 0
    second = read_page(
        first["locator"], offset=first["next_offset"], limit=1000,
        evidence_id=first["evidence_id"], expected_version=first["version"],
    )
    assert second["text"] == second["excerpt"] == LONG_TEXT[1000:2000]
    assert second["delivered_range"]["start"] == 1000

    search = web_search("q")
    assert search["state"] == "search_result" and search["results_empty"] is True
    assert "Confirm this request" in search["response_excerpt"]
    assert search["semantic_classification"] == "model-required"
