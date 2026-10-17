"""DeepSeek's standalone paid Search/Fetch, executed by the host, not a model.

The credential is read only inside this tool process. Neither request headers nor
provider error prose enter tool results. The existing capture/continuation ledger
is shared with the direct reader; provider Markdown is still untrusted evidence.
"""
import json
from pathlib import Path
from urllib.parse import urlparse

from .web_read import WebReader, MAX_RESULTS, public_host_refusal

SEARCH_ENDPOINT = "https://api.deepseek.com/v1/tools/search"
FETCH_ENDPOINT = "https://api.deepseek.com/v1/tools/fetch"


class DeepSeekWebReader(WebReader):
    def __init__(self, config, *, api_client_factory=None):
        super().__init__(config)
        self.api_client_factory = api_client_factory

    def _key(self):
        # Reuse the host's existing reference; never copy a key into tool config.
        try:
            for line in Path(self.config["credentials_file"]).read_text().splitlines():
                name, sep, value = line.partition("=")
                if sep and name.strip() == "EVENTMEM_API_KEY":
                    return value.strip().strip('"').strip("'")
        except (OSError, KeyError, TypeError):
            pass
        return None

    def _request(self, endpoint, payload):
        import httpx
        key = self._key()
        if not key:
            return None, "web-credential-unavailable"
        factory = self.api_client_factory or httpx.Client
        try:
            # Endpoint is a constant; redirects cannot forward the bearer token.
            with factory(timeout=30, follow_redirects=False, trust_env=False) as client:
                with client.stream("POST", endpoint, headers={"Authorization": "Bearer " + key}, json=payload) as response:
                    if response.status_code != 200:
                        return None, "web-service-http:" + str(response.status_code)
                    raw, over = self._bounded_body(response)
                    if over:
                        return None, "response-over-512k"
                    data = json.loads(raw)
                    if not isinstance(data, dict):
                        return None, "web-service-invalid-response"
                    return data, None
        except (httpx.HTTPError, OSError, ValueError):
            return None, "web-service-transport-or-response-error"

    def _fetch(self, url, **_):
        try:
            parsed = urlparse(url)
            if len(url) > 8192 or parsed.scheme not in {"http", "https"} or not parsed.hostname:
                return None, [], "unsupported-url-scheme"
            if parsed.username is not None or parsed.password is not None:
                return None, [], "credentials-in-url-refused"
            blocked = public_host_refusal(parsed.hostname, set(), self.config.get("public_transport"))
            if blocked:
                return None, [], blocked
        except ValueError:
            return None, [], "unsupported-url-scheme"
        data, error = self._request(FETCH_ENDPOINT, {"url": url})
        if error:
            return None, [], error
        if not isinstance(data.get("content"), str) or not isinstance(data.get("title", ""), str):
            return None, [], "web-service-invalid-response"
        # Fetch does not return the final redirected URL. Keep the requested URL,
        # and record that the provider, rather than a local GET, retrieved it.
        return {"body": data["content"].encode(), "title": data.get("title", ""),
                "headers": {"content-type": "text/markdown"}, "status_code": 200,
                "body_over_limit": False, "url": url, "encoding": "utf-8",
                "transport": {"kind": "deepseek-fetch", "final_url_verified": False}}, [], None

    @staticmethod
    def _readable(response, final):
        text, _, links, error = WebReader._readable(response, final)
        return text, response.get("title", ""), links, error

    def search(self, query, *, max_results=MAX_RESULTS, tool_call_id=None):
        error = None
        if not isinstance(query, str) or not query.strip() or len(query) > 128:
            error = "search-query-must-be-1-to-128-characters"
        if isinstance(max_results, bool) or not isinstance(max_results, int) or not 1 <= max_results <= MAX_RESULTS:
            error = "invalid-search-count"
        data = None
        if not error:
            data, error = self._request(SEARCH_ENDPOINT, {"query": query, "count": max_results, "need_content": False})
        if not error and (not isinstance(data.get("results"), list) or any(
                not isinstance(row, dict) or not all(isinstance(row.get(k), str) for k in ("title", "url"))
                for row in data["results"])):
            error = "web-service-invalid-response"
        if error:
            return self._receipt(tool="web_search", status="failed", requested=SEARCH_ENDPOINT,
                                 reason=error, tool_call_id=tool_call_id)
        results = [{"title": r["title"], "url": r["url"],
                    "snippet": r.get("snippet", ""), "summary": r.get("summary", ""), "date": r.get("date")}
                   for r in data["results"][:max_results]]
        return self._receipt(tool="web_search", status="search_result", requested=SEARCH_ENDPOINT,
                             tool_call_id=tool_call_id, extra={"query": query, "results": results,
                                 "results_empty": not results, "response_excerpt": "",
                                 "semantic_classification": "model-required", "provider": "deepseek-search",
                                 "note": "Search summaries are untrusted leads, not page reads. Call read_page before citing page content."})
