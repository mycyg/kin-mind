"""Destination-bound HTTP transport for public, read-only web requests.

The normal operating-system resolver may deliberately return synthetic
addresses (for example a TUN/FakeIP range).  This module does not relax the
public-address policy for those addresses.  Instead, an explicitly configured
DNS-over-HTTPS resolver is reached at configured bootstrap IPs, every returned
A/AAAA address is checked, and the HTTP connection is pinned to that exact
resolution snapshot while the original URL host remains the TLS SNI/Host.

The public entry point is :func:`create_public_client_factory`.  It returns
ordinary streaming ``httpx.Client`` instances, so response size limits remain
the caller's responsibility.  Redirect following is intentionally forbidden:
the caller must inspect each redirect and issue a new request, which triggers a
new resolution and destination binding.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import ssl
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

import certifi
import httpcore
import httpx

MAX_BOOTSTRAP_ADDRESSES = 4
MAX_DNS_ADDRESSES = 16
MAX_DOH_RESPONSE_BYTES = 64 * 1024
MAX_RESOLUTION_TIMEOUT_SECONDS = 10.0
MAX_TTL_SECONDS = 300


class PublicTransportError(RuntimeError):
    """A fail-closed policy or resolution error with a stable public reason."""

    def __init__(self, reason: str, detail: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class PublicResolution:
    """One verified resolution snapshot used by exactly one request."""

    hostname: str
    addresses: tuple[str, ...]
    resolver: str
    resolved_at: float
    expires_at: float


@dataclass(frozen=True)
class _TransportSettings:
    resolver_endpoint: str
    resolver_hostname: str
    resolver_bootstrap_addresses: tuple[str, ...]
    resolution_timeout_seconds: float
    max_ttl_seconds: int


@dataclass(frozen=True)
class _PinnedTarget:
    url: httpx.URL
    hostname: str
    port: int
    resolution: PublicResolution


def _public_address(value: Any) -> str:
    if not isinstance(value, str):
        raise PublicTransportError("address-not-public", "address-not-string")
    try:
        address = ipaddress.ip_address(value)
    except ValueError as error:
        raise PublicTransportError("address-not-public", "address-invalid") from error
    if (
        not address.is_global
        or address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    ):
        raise PublicTransportError("address-not-public", "address-policy")
    return str(address)


def _public_addresses(values: Iterable[Any], *, limit: int) -> tuple[str, ...]:
    unique: list[str] = []
    for value in values:
        address = _public_address(value)
        if address not in unique:
            unique.append(address)
        if len(unique) > limit:
            raise PublicTransportError("resolution-failed", "too-many-addresses")
    if not unique:
        raise PublicTransportError("host-unresolvable", "no-addresses")
    return tuple(unique)


def _url_parts(value: str | httpx.URL) -> tuple[httpx.URL, str, int]:
    try:
        url = value if isinstance(value, httpx.URL) else httpx.URL(value)
    except Exception as error:
        raise PublicTransportError("unsupported-url-scheme", "url-invalid") from error
    if url.scheme not in {"http", "https"} or not url.raw_host:
        raise PublicTransportError("unsupported-url-scheme", "scheme-or-host")
    if url.userinfo:
        raise PublicTransportError("credentials-in-url-refused")
    hostname = url.raw_host.decode("ascii").lower()
    port = url.port or (443 if url.scheme == "https" else 80)
    return url, hostname, port


def _number(
    value: Any,
    *,
    default: float,
    minimum: float,
    maximum: float,
    detail: str,
) -> float:
    candidate = default if value is None else value
    if isinstance(candidate, bool) or not isinstance(candidate, (int, float)):
        raise PublicTransportError("transport-config-invalid", detail)
    result = float(candidate)
    if not minimum <= result <= maximum:
        raise PublicTransportError("transport-config-invalid", detail)
    return result


def _settings_from_config(config: Mapping[str, Any]) -> _TransportSettings:
    if not isinstance(config, Mapping) or config.get("enabled") is not True:
        raise PublicTransportError("transport-config-invalid", "not-explicitly-enabled")
    resolver = config.get("resolver")
    if not isinstance(resolver, Mapping) or resolver.get("kind") != "doh-json":
        raise PublicTransportError("transport-config-invalid", "resolver-kind")
    endpoint_value = resolver.get("endpoint")
    if not isinstance(endpoint_value, str):
        raise PublicTransportError("transport-config-invalid", "resolver-endpoint")
    try:
        endpoint, hostname, port = _url_parts(endpoint_value)
    except PublicTransportError as error:
        raise PublicTransportError(
            "transport-config-invalid", "resolver-endpoint"
        ) from error
    if (
        endpoint.scheme != "https"
        or endpoint.userinfo
        or endpoint.fragment
        or endpoint.query
    ):
        raise PublicTransportError("transport-config-invalid", "resolver-endpoint")
    if port != 443:
        raise PublicTransportError("transport-config-invalid", "resolver-port")
    raw_bootstrap = resolver.get("bootstrap_addresses")
    if (
        not isinstance(raw_bootstrap, (list, tuple))
        or not raw_bootstrap
        or len(raw_bootstrap) > MAX_BOOTSTRAP_ADDRESSES
    ):
        raise PublicTransportError("transport-config-invalid", "resolver-bootstrap")
    try:
        bootstrap = _public_addresses(raw_bootstrap, limit=MAX_BOOTSTRAP_ADDRESSES)
    except PublicTransportError as error:
        raise PublicTransportError(
            "transport-config-invalid", "resolver-bootstrap"
        ) from error
    timeout = _number(
        config.get("resolution_timeout_seconds"),
        default=5.0,
        minimum=0.1,
        maximum=MAX_RESOLUTION_TIMEOUT_SECONDS,
        detail="resolution-timeout",
    )
    ttl_number = _number(
        config.get("max_ttl_seconds"),
        default=60,
        minimum=1,
        maximum=MAX_TTL_SECONDS,
        detail="max-ttl",
    )
    if not ttl_number.is_integer():
        raise PublicTransportError("transport-config-invalid", "max-ttl")
    return _TransportSettings(
        resolver_endpoint=str(endpoint),
        resolver_hostname=hostname,
        resolver_bootstrap_addresses=bootstrap,
        resolution_timeout_seconds=timeout,
        max_ttl_seconds=int(ttl_number),
    )


def _ssl_context() -> ssl.SSLContext:
    # certifi is an httpx dependency.  Supplying its CA file explicitly avoids
    # SSL_CERT_FILE/SSL_CERT_DIR environment overrides as well as proxy env.
    return ssl.create_default_context(cafile=certifi.where())


def _peer_address(stream: Any) -> tuple[str, int] | None:
    peer = stream.get_extra_info("server_addr") if stream is not None else None
    if not isinstance(peer, tuple) or len(peer) < 2:
        return None
    try:
        address = ipaddress.ip_address(str(peer[0]))
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        port = int(peer[1])
    except (TypeError, ValueError):
        return None
    return str(address), port


class _PinnedNetworkBackend(httpcore.NetworkBackend):
    """Replace hostname lookup with connections to one validated IP snapshot."""

    def __init__(
        self, target: _PinnedTarget, delegate: httpcore.NetworkBackend | None = None
    ):
        self._target = target
        self._delegate = delegate or httpcore.SyncBackend()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        if host.lower() != self._target.hostname or port != self._target.port:
            raise PublicTransportError("peer-address-mismatch", "unexpected-origin")
        last_error: Exception | None = None
        for address in self._target.resolution.addresses:
            try:
                stream = self._delegate.connect_tcp(
                    host=address,
                    port=port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as error:
                last_error = error
                continue
            peer = _peer_address(stream)
            if peer != (address, port):
                stream.close()
                raise PublicTransportError("peer-address-mismatch", "socket-peer")
            return stream
        if last_error is not None:
            raise last_error
        raise PublicTransportError("resolution-failed", "no-connect-candidate")

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        raise PublicTransportError("peer-address-mismatch", "unix-socket-refused")

    def sleep(self, seconds: float) -> None:
        self._delegate.sleep(seconds)


_HTTPCORE_EXCEPTIONS: dict[type[Exception], type[httpx.HTTPError]] = {
    httpcore.ConnectTimeout: httpx.ConnectTimeout,
    httpcore.ReadTimeout: httpx.ReadTimeout,
    httpcore.WriteTimeout: httpx.WriteTimeout,
    httpcore.PoolTimeout: httpx.PoolTimeout,
    httpcore.ConnectError: httpx.ConnectError,
    httpcore.ReadError: httpx.ReadError,
    httpcore.WriteError: httpx.WriteError,
    httpcore.ProxyError: httpx.ProxyError,
    httpcore.UnsupportedProtocol: httpx.UnsupportedProtocol,
    httpcore.LocalProtocolError: httpx.LocalProtocolError,
    httpcore.RemoteProtocolError: httpx.RemoteProtocolError,
}


@contextlib.contextmanager
def _map_httpcore_exceptions() -> Iterator[None]:
    try:
        yield
    except Exception as error:
        mapped: type[httpx.HTTPError] | None = None
        for source, target in _HTTPCORE_EXCEPTIONS.items():
            if isinstance(error, source) and (
                mapped is None or issubclass(target, mapped)
            ):
                mapped = target
        if mapped is None:
            raise
        raise mapped(str(error)) from error


class _CoreResponseStream(httpx.SyncByteStream):
    def __init__(self, stream: Iterable[bytes]):
        self._stream = stream

    def __iter__(self) -> Iterator[bytes]:
        with _map_httpcore_exceptions():
            yield from self._stream

    def close(self) -> None:
        if hasattr(self._stream, "close"):
            with _map_httpcore_exceptions():
                self._stream.close()  # type: ignore[union-attr]


class _OwnedResponseStream(httpx.SyncByteStream):
    """Keep a one-request inner transport alive until its stream is closed."""

    def __init__(self, stream: httpx.SyncByteStream, owner: _PinnedHTTPTransport):
        self._stream = stream
        self._owner = owner
        self._closed = False

    def __iter__(self) -> Iterator[bytes]:
        try:
            yield from self._stream
        finally:
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._stream.close()
        finally:
            self._owner.close()


class _PinnedHTTPTransport(httpx.BaseTransport):
    """One-origin transport whose TCP backend only sees verified IP literals."""

    def __init__(
        self,
        target: _PinnedTarget,
        *,
        network_backend: httpcore.NetworkBackend | None = None,
    ):
        self._target = target
        backend = _PinnedNetworkBackend(target, network_backend)
        self._pool = httpcore.ConnectionPool(
            ssl_context=_ssl_context(),
            max_connections=1,
            max_keepalive_connections=0,
            keepalive_expiry=0,
            http1=True,
            http2=False,
            retries=0,
            network_backend=backend,
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        request_host = request.url.raw_host.decode("ascii").lower()
        request_port = request.url.port or (
            443 if request.url.scheme == "https" else 80
        )
        if request_host != self._target.hostname or request_port != self._target.port:
            raise PublicTransportError("peer-address-mismatch", "request-origin")
        if not isinstance(request.stream, httpx.SyncByteStream):
            raise PublicTransportError("transport-request-invalid", "stream-type")
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        with _map_httpcore_exceptions():
            response = self._pool.handle_request(core_request)
        stream = response.extensions.get("network_stream")
        peer = _peer_address(stream)
        if (
            peer is None
            or peer[0] not in self._target.resolution.addresses
            or peer[1] != self._target.port
        ):
            response.close()
            raise PublicTransportError("peer-address-mismatch", "response-peer")
        extensions = dict(response.extensions)
        extensions["kin_pinned_peer"] = peer[0]
        return httpx.Response(
            status_code=response.status,
            headers=response.headers,
            stream=_CoreResponseStream(response.stream),
            extensions=extensions,
        )

    def close(self) -> None:
        with _map_httpcore_exceptions():
            self._pool.close()


def _literal_or_resolved_target(
    url: httpx.URL,
    hostname: str,
    port: int,
    resolver: _DohResolver,
) -> _PinnedTarget:
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        resolution = resolver.resolve(hostname)
    else:
        address = _public_address(str(literal))
        moment = time.time()
        resolution = PublicResolution(
            hostname=hostname,
            addresses=(address,),
            resolver="literal",
            resolved_at=moment,
            expires_at=moment,
        )
    # Resolver implementations are never trusted to self-police their output.
    addresses = _public_addresses(resolution.addresses, limit=MAX_DNS_ADDRESSES)
    resolution = PublicResolution(
        hostname=hostname,
        addresses=addresses,
        resolver=resolution.resolver,
        resolved_at=resolution.resolved_at,
        expires_at=resolution.expires_at,
    )
    return _PinnedTarget(url=url, hostname=hostname, port=port, resolution=resolution)


class _DohResolver:
    def __init__(self, settings: _TransportSettings):
        self._settings = settings

    def _bootstrap_target(self) -> _PinnedTarget:
        endpoint, hostname, port = _url_parts(self._settings.resolver_endpoint)
        moment = time.time()
        resolution = PublicResolution(
            hostname=hostname,
            addresses=self._settings.resolver_bootstrap_addresses,
            resolver="configured-bootstrap",
            resolved_at=moment,
            expires_at=moment,
        )
        return _PinnedTarget(endpoint, hostname, port, resolution)

    def _query_json(
        self, hostname: str, record_type: str, timeout: float
    ) -> Mapping[str, Any]:
        transport = _PinnedHTTPTransport(self._bootstrap_target())
        try:
            with (
                httpx.Client(
                    transport=transport,
                    timeout=timeout,
                    follow_redirects=False,
                    trust_env=False,
                ) as client,
                client.stream(
                    "GET",
                    self._settings.resolver_endpoint,
                    params={"name": hostname, "type": record_type},
                    headers={"Accept": "application/dns-json"},
                ) as response,
            ):
                if response.status_code != 200:
                    raise PublicTransportError(
                        "resolution-failed", "resolver-http-status"
                    )
                media_type = (
                    (response.headers.get("content-type") or "")
                    .split(";", 1)[0]
                    .lower()
                )
                if media_type not in {"application/dns-json", "application/json"}:
                    raise PublicTransportError(
                        "resolution-failed", "resolver-content-type"
                    )
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_DOH_RESPONSE_BYTES:
                        raise PublicTransportError(
                            "resolution-failed", "resolver-response-too-large"
                        )
        except PublicTransportError:
            raise
        except (httpx.HTTPError, OSError, ValueError) as error:
            raise PublicTransportError(
                "resolution-failed", type(error).__name__
            ) from error
        try:
            document = json.loads(bytes(body))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PublicTransportError("resolution-failed", "resolver-json") from error
        if not isinstance(document, Mapping):
            raise PublicTransportError("resolution-failed", "resolver-schema")
        return document

    def resolve(self, hostname: str) -> PublicResolution:
        deadline = time.monotonic() + self._settings.resolution_timeout_seconds
        found: list[str] = []
        ttls: list[int] = []
        for record_type in ("A", "AAAA"):
            expected_type = 1 if record_type == "A" else 28
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PublicTransportError("resolution-failed", "resolver-timeout")
            document = self._query_json(hostname, record_type, remaining)
            if document.get("TC") is True:
                raise PublicTransportError("resolution-failed", "resolver-truncated")
            status = document.get("Status")
            if status == 3:
                raise PublicTransportError("host-unresolvable", "nxdomain")
            if status != 0:
                raise PublicTransportError("resolution-failed", "resolver-rcode")
            questions = document.get("Question")
            if questions is not None:
                if not isinstance(questions, list) or not questions:
                    raise PublicTransportError("resolution-failed", "resolver-question")
                names = {
                    str(question.get("name", "")).rstrip(".").lower()
                    for question in questions
                    if isinstance(question, Mapping)
                }
                types = {
                    question.get("type")
                    for question in questions
                    if isinstance(question, Mapping)
                }
                if names != {hostname.rstrip(".").lower()} or types != {expected_type}:
                    raise PublicTransportError("resolution-failed", "resolver-question")
            answers = document.get("Answer", [])
            if not isinstance(answers, list):
                raise PublicTransportError("resolution-failed", "resolver-answer")
            for answer in answers:
                if not isinstance(answer, Mapping):
                    raise PublicTransportError("resolution-failed", "resolver-answer")
                answer_type = answer.get("type")
                if answer_type not in {1, 28}:
                    continue
                if answer_type != expected_type:
                    raise PublicTransportError(
                        "resolution-failed", "resolver-unexpected-address-family"
                    )
                address = _public_address(answer.get("data"))
                parsed = ipaddress.ip_address(address)
                if (answer_type == 1) != isinstance(parsed, ipaddress.IPv4Address):
                    raise PublicTransportError(
                        "resolution-failed", "resolver-address-family"
                    )
                if address not in found:
                    found.append(address)
                ttl = answer.get("TTL", 0)
                if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl < 0:
                    raise PublicTransportError("resolution-failed", "resolver-ttl")
                ttls.append(ttl)
                if len(found) > MAX_DNS_ADDRESSES:
                    raise PublicTransportError(
                        "resolution-failed", "too-many-addresses"
                    )
        addresses = _public_addresses(found, limit=MAX_DNS_ADDRESSES)
        resolved_at = time.time()
        ttl = min([self._settings.max_ttl_seconds, *ttls]) if ttls else 0
        return PublicResolution(
            hostname=hostname,
            addresses=addresses,
            resolver=self._settings.resolver_hostname,
            resolved_at=resolved_at,
            expires_at=resolved_at + ttl,
        )


_CONSTRUCTION_TOKEN = object()


class PublicHttpTransport(httpx.BaseTransport):
    """Dynamic, read-only transport built only from validated explicit config."""

    def __init__(self, settings: _TransportSettings, *, _token: object):
        if _token is not _CONSTRUCTION_TOKEN:
            raise TypeError("use PublicHttpTransport.from_config")
        self._settings = settings
        self._resolver = _DohResolver(settings)

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> PublicHttpTransport:
        return cls(_settings_from_config(config), _token=_CONSTRUCTION_TOKEN)

    @classmethod
    def _from_settings(cls, settings: _TransportSettings) -> PublicHttpTransport:
        return cls(settings, _token=_CONSTRUCTION_TOKEN)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.method not in {"GET", "HEAD"}:
            raise PublicTransportError("method-not-read-only")
        url, hostname, port = _url_parts(request.url)
        target = _literal_or_resolved_target(url, hostname, port, self._resolver)
        inner = _PinnedHTTPTransport(target)
        try:
            response = inner.handle_request(request)
        except Exception:
            inner.close()
            raise
        metadata = {
            "hostname": hostname,
            "resolved_addresses": target.resolution.addresses,
            "peer_address": response.extensions["kin_pinned_peer"],
            "resolver": target.resolution.resolver,
            "resolved_at": target.resolution.resolved_at,
            "expires_at": target.resolution.expires_at,
        }
        extensions = dict(response.extensions)
        extensions["kin_public_transport"] = metadata
        return httpx.Response(
            status_code=response.status_code,
            headers=response.headers,
            stream=_OwnedResponseStream(response.stream, inner),
            extensions=extensions,
        )

    def close(self) -> None:
        # Each request owns its inner transport through _OwnedResponseStream.
        return None


def create_public_client_factory(
    config: Mapping[str, Any],
) -> Callable[..., httpx.Client]:
    """Validate config once and return the WebReader-compatible client factory."""

    settings = _settings_from_config(config)

    def create_client(*, timeout: Any, follow_redirects: bool = False) -> httpx.Client:
        if follow_redirects is not False:
            raise PublicTransportError("redirects-must-be-explicit")
        return httpx.Client(
            transport=PublicHttpTransport._from_settings(settings),
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        )

    return create_client


__all__ = [
    "PublicHttpTransport",
    "PublicResolution",
    "PublicTransportError",
    "create_public_client_factory",
]
