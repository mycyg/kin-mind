"""Read-only web tools for a bounded exploration executor: search and page fetch.

Pure HTTP, no Kimi, no extra model. Every call writes a receipt to the run's
ledger: search results are `search_result` evidence (proof the result was
visible, never proof the page was read); only a `read_page` receipt marked
`observed` by this trusted adapter means read succeeded. The model cannot write
the ledger. Nothing here sends messages or touches shared memory.
"""

from __future__ import annotations

import hashlib
import html.parser
import ipaddress
import json
import os
import re
import socket
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

from eventmem.core.models import now

# The sanctioned no-key search backend; overridable by host config.
DEFAULT_SEARCH_ENDPOINT = "https://html.duckduckgo.com/html/"
MAX_BYTES = 512 * 1024
MAX_TEXT = 16000
MAX_RESULTS = 8
MAX_REDIRECTS = 5
REQUEST_TIMEOUT = 15
PER_HOST_MIN_INTERVAL = 1.0
PER_HOST_MAX_REQUESTS = 4

RESULT_LINK = re.compile(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL)
RESULT_SNIPPET = re.compile(r'class="result__snippet"[^>]*>(.*?)</a>', re.DOTALL)
TAG = re.compile(r"<[^>]+>")


class _TextPage(html.parser.HTMLParser):
    """Readable extraction for HTML: title, visible text, outbound links."""

    def __init__(self, base):
        super().__init__()
        self.base = base
        self.skip = 0
        self.title = ""
        self._in_title = False
        self.text = []
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "template"}:
            self.skip += 1
        if tag == "title":
            self._in_title = True
        if tag == "a" and not self.skip:
            href = dict(attrs).get("href")
            if href:
                self.links.append(urljoin(self.base, href))

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "template"} and self.skip:
            self.skip -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if not self.skip and data.strip():
            self.text.append(data.strip())


def _unhtml(value):
    return re.sub(r"\s+", " ", TAG.sub("", value)).strip()


def _blocked_address(host, allow_hosts):
    """Resolve and refuse loopback/private/link-local/reserved targets."""
    if host in allow_hosts:
        return False
    try:
        answers = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return "host-unresolvable"
    for answer in answers:
        address = ipaddress.ip_address(answer[4][0])
        if address.is_loopback or address.is_private or address.is_link_local or address.is_reserved or address.is_multicast:
            return "address-not-public"
    return False


class WebReader:
    """One run's web reads. The ledger file is written by this adapter only."""

    def __init__(self, config):
        self.config = config
        self.ledger = Path(config["ledger"])
        self.lock = threading.Lock()
        self.allow_hosts = set(config.get("allow_hosts", []))
        self._last_request = {}
        self._host_requests = {}

    def _record(self, entry):
        with self.lock:
            data = json.loads(self.ledger.read_text()) if self.ledger.exists() else {}
            data[entry["evidence_id"]] = entry
            self.ledger.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = self.ledger.with_suffix(".tmp")
            temporary.write_text(json.dumps(data, ensure_ascii=False))
            temporary.chmod(0o600)
            temporary.replace(self.ledger)
        return entry

    def _receipt(self, *, tool, status, requested, final=None, text="", title="", reason=None,
                 tool_call_id=None, extra=None):
        version = hashlib.sha256(text.encode()).hexdigest() if text else None
        evidence_id = "web_" + hashlib.sha256(
            (self.config["execution_id"] + "\0" + str(requested) + "\0" + str(version) + "\0" + now()).encode()
        ).hexdigest()[:32]
        entry = {"evidence_id": evidence_id, "execution_id": self.config["execution_id"],
                 "attempt": self.config["attempt"], "tool": tool, "tool_call_id": tool_call_id,
                 "state": status, "requested_locator": requested,
                 "locator": final or requested, "title": title,
                 "version": version, "read_at": now(), "excerpt": text[:2000],
                 "truncated": len(text) > MAX_TEXT, "failure_reason": reason, **(extra or {})}
        return self._record(entry)

    def _fetch(self, url, *, allow_redirects):
        """Bounded GET with explicit per-hop redirect policy and SSRF checks."""
        import httpx

        chain = []
        current = url
        for _ in range(allow_redirects + 1):
            parsed = urlparse(current)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                return None, chain, "unsupported-url-scheme"
            if parsed.username or parsed.password:
                return None, chain, "credentials-in-url-refused"
            blocked = _blocked_address(parsed.hostname, self.allow_hosts)
            if blocked:
                return None, chain, blocked
            host = parsed.hostname
            self._host_requests[host] = self._host_requests.get(host, 0) + 1
            if self._host_requests[host] > PER_HOST_MAX_REQUESTS:
                return None, chain, "host-courtesy-limit"
            waited = time.monotonic() - self._last_request.get(host, 0)
            if waited < PER_HOST_MIN_INTERVAL:
                time.sleep(PER_HOST_MIN_INTERVAL - waited)
            self._last_request[host] = time.monotonic()
            try:
                with httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=False) as client:
                    response = client.get(current, headers={"User-Agent": "kin-exploration-reader/1.0"})
            except Exception:  # noqa: BLE001 - network failures become failed receipts
                return None, chain, "fetch-failed"
            if response.is_redirect:
                target = urljoin(current, response.headers["location"])
                chain.append({"from": current, "to": target, "status": response.status_code})
                current = target
                continue
            return response, chain, None
        return None, chain, "redirect-limit-exceeded"

    def read_page(self, url, *, tool_call_id=None):
        """Read one public page. Only an `observed` receipt here means read succeeded."""
        response, chain, error = self._fetch(url, allow_redirects=MAX_REDIRECTS)
        if error:
            return self._receipt(tool="read_page", status="failed", requested=url, reason=error,
                                 tool_call_id=tool_call_id, extra={"redirect_chain": chain})
        content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
        raw = response.content[: MAX_BYTES + 1]
        if len(raw) > MAX_BYTES:
            return self._receipt(tool="read_page", status="failed", requested=url, reason="response-over-512k",
                                 tool_call_id=tool_call_id, extra={"redirect_chain": chain})
        final = str(response.url)
        if content_type == "text/html":
            page = _TextPage(final)
            page.feed(raw.decode(response.encoding or "utf-8", errors="replace"))
            text, title = "\n".join(page.text)[:MAX_TEXT], page.title.strip()
            links = [link for link in page.links[:100]]
        elif content_type.startswith("text/") or content_type == "application/json":
            text, title = raw.decode("utf-8", errors="replace")[:MAX_TEXT], ""
            links = []
        else:
            return self._receipt(tool="read_page", status="failed", requested=url, final=final,
                                 reason="unsupported-content-type:" + (content_type or "unknown"),
                                 tool_call_id=tool_call_id, extra={"redirect_chain": chain})
        return self._receipt(tool="read_page", status="observed", requested=url, final=final,
                             text=text, title=title, tool_call_id=tool_call_id,
                             extra={"content_type": content_type, "redirect_chain": chain,
                                    "links": links})

    def search(self, query, *, max_results=MAX_RESULTS, tool_call_id=None):
        """Search the configured no-key endpoint. Results prove visibility only."""
        endpoint = self.config.get("search_endpoint") or DEFAULT_SEARCH_ENDPOINT
        blocked = _blocked_address(urlparse(endpoint).hostname or "", self.allow_hosts)
        if blocked:
            return self._receipt(tool="web_search", status="failed", requested=endpoint,
                                 reason=blocked, tool_call_id=tool_call_id)
        response, chain, error = self._fetch(endpoint + "?q=" + quote_plus(query), allow_redirects=2)
        # redirect chain recorded below
        if error:
            return self._receipt(tool="web_search", status="failed", requested=endpoint,
                                 reason=error, tool_call_id=tool_call_id, extra={"query": query})
        text = response.content[:MAX_BYTES].decode("utf-8", errors="replace")
        links = RESULT_LINK.findall(text)
        snippets = [_unhtml(s) for s in RESULT_SNIPPET.findall(text)]
        results = []
        for index, (href, label) in enumerate(links[:max_results]):
            target = href
            if "uddg=" in href:
                target = unquote(parse_qs(urlparse(href).query).get("uddg", [href])[0])
            results.append({"title": _unhtml(label), "url": target,
                            "snippet": snippets[index] if index < len(snippets) else ""})
        return self._receipt(tool="web_search", status="search_result", requested=endpoint,
                             final=str(response.url), tool_call_id=tool_call_id,
                             extra={"query": query, "redirect_chain": chain, "results": results,
                                    "note": "A search result is proof of visibility, never of having read the page."})


def create_server(config):
    from mcp.server.fastmcp import FastMCP
    server = FastMCP("kin_web")
    reader = WebReader(config)

    @server.tool()
    def web_search(query: str, max_results: int = 5) -> dict:
        """Search the configured public endpoint. Results prove a page was visible in
        results, never that it was read — call read_page before citing content."""
        receipt = reader.search(query, max_results=max(1, min(MAX_RESULTS, max_results)))
        if receipt["state"] != "search_result":
            return {"state": "failed", "reason": receipt["failure_reason"], "results": []}
        return {"state": "search_result", "evidence_id": receipt["evidence_id"],
                "results": receipt["results"], "note": receipt["note"]}

    @server.tool()
    def read_page(url: str) -> dict:
        """Read one public page (HTML/text/JSON, bounded). Returns the content with a
        receipt: evidence_id, requested vs final locator, version hash, truncation.
        Only this receipt marks the page actually read; cite its locator exactly."""
        receipt = reader.read_page(url)
        if receipt["state"] != "observed":
            return {"state": "failed", "reason": receipt["failure_reason"], "evidence_id": receipt["evidence_id"]}
        return {"state": "observed", "evidence_id": receipt["evidence_id"], "locator": receipt["locator"],
                "requested_locator": receipt["requested_locator"], "title": receipt["title"],
                "version": receipt["version"], "truncated": receipt["truncated"],
                "text": receipt["excerpt"] if receipt["truncated"] else None,
                "excerpt": receipt["excerpt"]}

    return server


if __name__ == "__main__":
    import sys
    os.umask(0o077)
    create_server(json.loads(Path(sys.argv[1]).read_text())).run()
