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
EXCERPT_CHARS = 2000
MAX_DELIVERED_RANGES = 64
MAX_RESULTS = 8
MAX_REDIRECTS = 5
REQUEST_TIMEOUT = 15
PER_HOST_MIN_INTERVAL = 1.0
PER_HOST_MAX_REQUESTS = 4

RESULT_LINK = re.compile(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL)
RESULT_SNIPPET = re.compile(r'class="result__snippet"[^>]*>(.*?)</a>', re.DOTALL)
TAG = re.compile(r"<[^>]+>")
EVIDENCE_ID = re.compile(r"^web_[0-9a-f]{32}$")


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

    def __init__(self, config, *, client_factory=None, controlled_transport=False):
        self.config = config
        self.ledger = Path(config["ledger"])
        self.lock = threading.Lock()
        self.allow_hosts = set(config.get("allow_hosts", []))
        transport_config = config.get("public_transport")
        if transport_config is not None and transport_config is not False and not isinstance(transport_config, dict):
            raise TypeError("transport-config-invalid")
        enabled = False
        if isinstance(transport_config, dict):
            enabled_value = transport_config.get("enabled", False)
            if not isinstance(enabled_value, bool):
                raise TypeError("transport-config-invalid")
            enabled = enabled_value
        # A controlled connector injects an httpx-compatible client factory.
        # Only explicit public_transport.enabled=true changes the legacy route.
        if enabled:
            controlled_transport = True
            if client_factory is None:
                from .http_transport import create_public_client_factory
                client_factory = create_public_client_factory(transport_config)
        if controlled_transport and client_factory is None:
            raise ValueError("transport-config-invalid")
        self.client_factory = client_factory
        self.controlled_transport = controlled_transport
        self._last_request = {}
        self._host_requests = {}

    def _write_ledger_unlocked(self, data):
        self.ledger.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.ledger.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False))
        temporary.chmod(0o600)
        temporary.replace(self.ledger)

    def _record(self, entry):
        with self.lock:
            data = json.loads(self.ledger.read_text()) if self.ledger.exists() else {}
            data[entry["evidence_id"]] = entry
            self._write_ledger_unlocked(data)
        return entry

    def _content_path(self, evidence_id):
        if not EVIDENCE_ID.fullmatch(str(evidence_id or "")):
            raise ValueError("invalid-evidence-id")
        return self.ledger.parent / "web-content" / (evidence_id + ".txt")

    def _write_content(self, evidence_id, text):
        target = self._content_path(evidence_id)
        if target.parent.exists() and target.parent.is_symlink():
            raise ValueError("content-storage-unavailable")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.parent.chmod(0o700)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(text, encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(target)
        return str(Path("web-content") / target.name)

    def _receipt(self, *, tool, status, requested, final=None, text="", title="", reason=None,
                 tool_call_id=None, extra=None, persist_text=False):
        version = hashlib.sha256(text.encode()).hexdigest() if text else None
        recorded_at = now()
        evidence_id = "web_" + hashlib.sha256(
            (self.config["execution_id"] + "\0" + str(requested) + "\0" + str(version) + "\0" + recorded_at).encode()
        ).hexdigest()[:32]
        entry = {"evidence_id": evidence_id, "execution_id": self.config["execution_id"],
                 "receipt_format": "kin-web-receipt-v2",
                 "attempt": self.config["attempt"], "tool": tool, "tool_call_id": tool_call_id,
                 "state": status, "requested_locator": requested,
                 "locator": final or requested, "title": title,
                 "version": version, "read_at": recorded_at, "excerpt": text[:EXCERPT_CHARS],
                 "truncated": len(text) > MAX_TEXT, "failure_reason": reason, **(extra or {})}
        if persist_text:
            entry.update(content_ref=self._write_content(evidence_id, text),
                         content_sha256=version, content_chars=len(text))
        return self._record(entry)

    @staticmethod
    def _page(text, offset, limit):
        end = min(len(text), offset + limit)
        delivered = text[offset:end]
        delivered_range = {"start": offset, "end": end,
                           "sha256": hashlib.sha256(delivered.encode()).hexdigest()}
        return {"text": delivered, "delivered_range": delivered_range,
                "next_offset": end if end < len(text) else None,
                "truncated": end < len(text),
                "content_complete": offset == 0 and end == len(text)}

    @staticmethod
    def _valid_page_request(offset, limit):
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            return "invalid-page-offset"
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_TEXT:
            return "invalid-page-limit"
        return None

    def _client(self):
        if self.client_factory is not None:
            return self.client_factory(timeout=REQUEST_TIMEOUT, follow_redirects=False)
        import httpx
        return httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=False, trust_env=False)

    @staticmethod
    def _bounded_body(response):
        """Read at most 512 KiB plus one of decoded HTTP entity bytes.

        ``httpx.Response.iter_bytes`` applies Content-Encoding decoders, so this
        bound and the associated digest intentionally describe the decoded entity,
        not compressed wire bytes.
        """
        declared = response.headers.get("content-length")
        if declared:
            try:
                if int(declared) > MAX_BYTES:
                    return b"", True
            except ValueError:
                pass
        body = bytearray()
        for chunk in response.iter_bytes(chunk_size=64 * 1024):
            if not chunk:
                continue
            remaining = MAX_BYTES + 1 - len(body)
            if remaining <= 0:
                break
            body.extend(chunk[:remaining])
            if len(body) > MAX_BYTES:
                return bytes(body), True
        return bytes(body), False

    def _continuation_failure(self, url, reason, *, evidence_id=None, expected_version=None,
                              tool_call_id=None, extra=None):
        return self._receipt(
            tool="read_page", status="failed", requested=url, reason=reason,
            tool_call_id=tool_call_id,
            extra={"source_evidence_id": evidence_id, "expected_version": expected_version,
                   **(extra or {})},
        )

    def _continue_page(self, url, *, evidence_id, expected_version, offset, limit,
                       tool_call_id=None):
        if not EVIDENCE_ID.fullmatch(str(evidence_id or "")):
            return self._continuation_failure(
                url, "continuation-not-found", evidence_id=evidence_id,
                expected_version=expected_version, tool_call_id=tool_call_id,
            )
        if not isinstance(expected_version, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_version):
            return self._continuation_failure(
                url, "expected-version-required", evidence_id=evidence_id,
                expected_version=expected_version, tool_call_id=tool_call_id,
            )
        reason = None
        source = None
        text = None
        page = None
        with self.lock:
            data = json.loads(self.ledger.read_text()) if self.ledger.exists() else {}
            source = data.get(evidence_id)
            if (not isinstance(source, dict) or source.get("execution_id") != self.config["execution_id"]
                    or source.get("attempt") != self.config["attempt"]
                    or source.get("tool") != "read_page" or source.get("state") != "observed"):
                reason = "continuation-not-found"
            elif url not in {source.get("requested_locator"), source.get("locator")}:
                reason = "continuation-source-mismatch"
            elif source.get("version") != expected_version or source.get("content_sha256") != expected_version:
                reason = "source-version-changed"
            else:
                target = self._content_path(evidence_id)
                expected_ref = str(Path("web-content") / target.name)
                if (source.get("content_ref") != expected_ref or not target.is_file()
                        or target.is_symlink() or target.parent.is_symlink()
                        or target.stat().st_size > MAX_BYTES * 4):
                    reason = "content-unavailable"
                else:
                    text = target.read_text(encoding="utf-8")
                    if (len(text) != source.get("content_chars")
                            or hashlib.sha256(text.encode()).hexdigest() != expected_version):
                        reason = "content-integrity-failed"
                    elif offset >= len(text):
                        reason = "page-offset-out-of-range"
                    else:
                        page = self._page(text, offset, limit)
                        delivered = {**page["delivered_range"], "delivered_at": now()}
                        delivered_at = now()
                        ranges = list(source.get("delivered_ranges") or [])
                        identity = {key: delivered[key] for key in ("start", "end", "sha256")}
                        existing = next((item for item in ranges if isinstance(item, dict) and
                            all(item.get(key) == value for key, value in identity.items())
                        ), None)
                        if existing is None and len(ranges) >= MAX_DELIVERED_RANGES:
                            reason = "delivery-range-limit"
                        else:
                            if existing is None:
                                ranges.append(delivered)
                            else:
                                delivered = existing
                            page["delivered_range"] = delivered
                            source = {**source, "delivered_ranges": ranges,
                                      "last_delivered_at": delivered_at}
                            data[evidence_id] = source
                            self._write_ledger_unlocked(data)
        if reason:
            return self._continuation_failure(
                url, reason, evidence_id=evidence_id, expected_version=expected_version,
                tool_call_id=tool_call_id,
            )
        return {**source, **page}

    def _fetch(self, url, *, allow_redirects):
        """Bounded streaming GET with explicit per-hop redirect policy."""
        chain = []
        current = url
        try:
            with self._client() as client:
                for _ in range(allow_redirects + 1):
                    parsed = urlparse(current)
                    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                        return None, chain, "unsupported-url-scheme"
                    if parsed.username or parsed.password:
                        return None, chain, "credentials-in-url-refused"
                    # A configured PublicHttpTransport owns DNS resolution, public-IP
                    # validation and peer pinning. Running socket.getaddrinfo first would
                    # reject a safe pinned route on Fake-IP systems.
                    if not self.controlled_transport:
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
                    with client.stream(
                        "GET", current, headers={"User-Agent": "kin-exploration-reader/1.0"},
                    ) as response:
                        transport = (getattr(response, "extensions", {}) or {}).get("kin_public_transport")
                        if response.is_redirect:
                            location = response.headers.get("location")
                            if not location:
                                return None, chain, "redirect-location-missing"
                            target = urljoin(current, location)
                            hop = {"from": current, "to": target, "status": response.status_code}
                            if isinstance(transport, dict):
                                hop["transport"] = transport
                            chain.append(hop)
                            current = target
                            continue
                        raw, over_limit = self._bounded_body(response)
                        return {
                            "status_code": response.status_code,
                            "headers": {str(key).lower(): str(value)
                                        for key, value in response.headers.items()},
                            "url": str(response.url),
                            "encoding": response.encoding or "utf-8",
                            "body": raw,
                            "body_over_limit": over_limit,
                            "transport": transport if isinstance(transport, dict) else None,
                        }, chain, None
        except Exception as error:  # noqa: BLE001 - network failures become bounded receipts
            reason = getattr(error, "reason", None) if self.controlled_transport else None
            if not isinstance(reason, str) or not re.fullmatch(r"[a-z0-9-]{1,80}", reason):
                reason = "fetch-failed"
            return None, chain, reason
        return None, chain, "redirect-limit-exceeded"

    @staticmethod
    def _content_type(response):
        return (response["headers"].get("content-type") or "").split(";")[0].strip().lower()

    @staticmethod
    def _readable(response, final):
        content_type = WebReader._content_type(response)
        raw = response["body"]
        decoded = raw.decode(response.get("encoding") or "utf-8", errors="replace")
        if content_type == "text/html":
            page = _TextPage(final)
            page.feed(decoded)
            return "\n".join(page.text), page.title.strip(), page.links[:100], None
        if content_type.startswith("text/") or content_type == "application/json":
            return decoded, "", [], None
        return None, "", [], "unsupported-content-type:" + (content_type or "unknown")

    @staticmethod
    def _response_facts(response, chain):
        raw = response["body"]
        facts = {
            "http_status": response["status_code"],
            "content_type": WebReader._content_type(response),
            "redirect_chain": chain,
            "raw_body_bytes": len(raw),
            "raw_body_sha256": hashlib.sha256(raw).hexdigest(),
            "raw_body_complete": not response["body_over_limit"],
            "body_bytes_representation": "decoded-http-entity",
        }
        if response.get("transport"):
            facts["transport"] = response["transport"]
        return facts

    def read_page(self, url, *, offset=0, limit=MAX_TEXT, evidence_id=None,
                  expected_version=None, tool_call_id=None):
        """Read or continue one public page with an exact delivered character range."""
        invalid = self._valid_page_request(offset, limit)
        if invalid:
            return self._continuation_failure(
                url, invalid, evidence_id=evidence_id, expected_version=expected_version,
                tool_call_id=tool_call_id,
            )
        if evidence_id is not None:
            return self._continue_page(
                url, evidence_id=evidence_id, expected_version=expected_version,
                offset=offset, limit=limit, tool_call_id=tool_call_id,
            )
        if offset:
            return self._continuation_failure(
                url, "continuation-evidence-required", expected_version=expected_version,
                tool_call_id=tool_call_id,
            )
        response, chain, error = self._fetch(url, allow_redirects=MAX_REDIRECTS)
        if error:
            return self._receipt(
                tool="read_page", status="failed", requested=url, reason=error,
                tool_call_id=tool_call_id, extra={"redirect_chain": chain},
            )
        facts = self._response_facts(response, chain)
        final = response["url"]
        if response["status_code"] != 200:
            text, title, _links, _ = self._readable(response, final)
            return self._receipt(
                tool="read_page", status="failed", requested=url, final=final,
                text=text or "", title=title, reason="http-status:" + str(response["status_code"]),
                tool_call_id=tool_call_id, extra=facts,
            )
        if response["body_over_limit"]:
            return self._receipt(
                tool="read_page", status="failed", requested=url, final=final,
                reason="response-over-512k", tool_call_id=tool_call_id, extra=facts,
            )
        text, title, links, content_error = self._readable(response, final)
        if content_error:
            return self._receipt(
                tool="read_page", status="failed", requested=url, final=final,
                reason=content_error, tool_call_id=tool_call_id, extra=facts,
            )
        if not text or not text.strip():
            return self._receipt(
                tool="read_page", status="failed", requested=url, final=final,
                title=title, reason="empty-content", tool_call_id=tool_call_id, extra=facts,
            )
        version = hashlib.sha256(text.encode()).hexdigest()
        if expected_version is not None and expected_version != version:
            return self._receipt(
                tool="read_page", status="failed", requested=url, final=final,
                text=text, title=title, reason="source-version-changed", tool_call_id=tool_call_id,
                extra={**facts, "expected_version": expected_version, "observed_version": version},
            )
        page = self._page(text, 0, limit)
        delivered = {**page["delivered_range"], "delivered_at": now()}
        page["delivered_range"] = delivered
        delivered_at = now()
        entry = self._receipt(
            tool="read_page", status="observed", requested=url, final=final,
            text=text, title=title, tool_call_id=tool_call_id, persist_text=True,
            extra={
                **facts,
                "links": links,
                "truncated": page["truncated"],
                "content_complete": page["content_complete"],
                "next_offset": page["next_offset"],
                "delivered_ranges": [delivered],
                "last_delivered_at": delivered_at,
                "semantic_classification": "model-required",
                "semantic_note": (
                    "HTTP success proves only that this text was read. Classify login, challenge, "
                    "consent and error-shell pages from the delivered text before treating it as an article."
                ),
            },
        )
        return {**entry, **page, "delivered_range": delivered}

    def search(self, query, *, max_results=MAX_RESULTS, tool_call_id=None):
        """Search the configured no-key endpoint. Results prove visibility only."""
        endpoint = self.config.get("search_endpoint") or DEFAULT_SEARCH_ENDPOINT
        response, chain, error = self._fetch(endpoint + "?q=" + quote_plus(query), allow_redirects=2)
        if error:
            return self._receipt(tool="web_search", status="failed", requested=endpoint,
                                 reason=error, tool_call_id=tool_call_id,
                                 extra={"query": query, "redirect_chain": chain})
        facts = self._response_facts(response, chain)
        if response["status_code"] != 200:
            return self._receipt(
                tool="web_search", status="failed", requested=endpoint, final=response["url"],
                reason="http-status:" + str(response["status_code"]), tool_call_id=tool_call_id,
                extra={"query": query, **facts},
            )
        if response["body_over_limit"]:
            return self._receipt(
                tool="web_search", status="failed", requested=endpoint, final=response["url"],
                reason="response-over-512k", tool_call_id=tool_call_id,
                extra={"query": query, **facts},
            )
        text = response["body"].decode(response.get("encoding") or "utf-8", errors="replace")
        links = RESULT_LINK.findall(text)
        snippets = [_unhtml(s) for s in RESULT_SNIPPET.findall(text)]
        results = []
        for index, (href, label) in enumerate(links[:max_results]):
            target = href
            if "uddg=" in href:
                target = unquote(parse_qs(urlparse(href).query).get("uddg", [href])[0])
            results.append({"title": _unhtml(label), "url": target,
                            "snippet": snippets[index] if index < len(snippets) else ""})
        visible_page = _TextPage(response["url"])
        visible_page.feed(text)
        response_excerpt = "\n".join(visible_page.text)[:EXCERPT_CHARS]
        return self._receipt(tool="web_search", status="search_result", requested=endpoint,
                             final=response["url"], tool_call_id=tool_call_id,
                             extra={"query": query, **facts, "results": results,
                                    "results_empty": not results,
                                    "response_excerpt": response_excerpt,
                                    "semantic_classification": "model-required",
                                    "note": (
                                        "A search result is proof of visibility, never of having read the page. "
                                        "Classify an empty-result response from its excerpt rather than treating "
                                        "it as evidence that no results exist."
                                    )})


def create_server(config):
    from mcp.server.fastmcp import FastMCP
    server = FastMCP("kin_web")
    reader = WebReader(config)

    @server.tool()
    def web_search(query: str, max_results: int = 5) -> dict:
        """搜索当前公开检索入口。结果只证明页面出现在搜索中；引用正文前先调用 read_page。"""
        receipt = reader.search(query, max_results=max(1, min(MAX_RESULTS, max_results)))
        if receipt["state"] != "search_result":
            return {"state": "failed", "reason": receipt["failure_reason"], "results": []}
        return {"state": "search_result", "evidence_id": receipt["evidence_id"],
                "results": receipt["results"], "results_empty": receipt["results_empty"],
                "response_excerpt": receipt["response_excerpt"],
                "semantic_classification": receipt["semantic_classification"],
                "note": receipt["note"]}

    @server.tool()
    def read_page(url: str, offset: int = 0, limit: int = MAX_TEXT,
                  evidence_id: str | None = None, expected_version: str | None = None) -> dict:
        """分段读取公开 HTML、文本或 JSON。首次读取冻结可读正文；之后沿 continuation 字段读取其他字符范围，避免混用版本。HTTP 成功只证明取到了返回文本；先辨认登录、验证、同意或错误页，再引用确切 locator。"""
        receipt = reader.read_page(
            url, offset=offset, limit=limit, evidence_id=evidence_id,
            expected_version=expected_version,
        )
        if receipt["state"] != "observed":
            return {"state": "failed", "reason": receipt["failure_reason"],
                    "evidence_id": receipt["evidence_id"], "locator": receipt["locator"],
                    "http_status": receipt.get("http_status")}
        continuation = None
        if receipt["next_offset"] is not None:
            continuation = {"url": receipt["locator"], "offset": receipt["next_offset"],
                            "evidence_id": receipt["evidence_id"],
                            "expected_version": receipt["version"]}
        return {
            "state": "observed",
            "receipt_format": receipt["receipt_format"],
            "evidence_id": receipt["evidence_id"],
            "locator": receipt["locator"],
            "requested_locator": receipt["requested_locator"],
            "title": receipt["title"],
            "version": receipt["version"],
            "content_sha256": receipt["content_sha256"],
            "content_chars": receipt["content_chars"],
            "raw_body_sha256": receipt["raw_body_sha256"],
            "raw_body_bytes": receipt["raw_body_bytes"],
            "body_bytes_representation": receipt["body_bytes_representation"],
            "http_status": receipt["http_status"],
            "content_type": receipt["content_type"],
            "semantic_classification": receipt["semantic_classification"],
            "semantic_note": receipt["semantic_note"],
            "delivered_range": receipt["delivered_range"],
            "text": receipt["text"],
            # Keep the public excerpt inside the exact range recorded as delivered.
            # The ledger's first-page excerpt remains available for audit only.
            "excerpt": receipt["text"][:EXCERPT_CHARS],
            "truncated": receipt["truncated"],
            "content_complete": receipt["content_complete"],
            "next_offset": receipt["next_offset"],
            "continuation": continuation,
        }

    return server


if __name__ == "__main__":
    import sys
    os.umask(0o077)
    create_server(json.loads(Path(sys.argv[1]).read_text())).run()
