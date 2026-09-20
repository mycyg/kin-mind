from __future__ import annotations

import httpcore
import httpx
import pytest

from kin_mind import http_transport as transport

PUBLIC_CONFIG = {
    "enabled": True,
    "resolver": {
        "kind": "doh-json",
        "endpoint": "https://cloudflare-dns.com/dns-query",
        "bootstrap_addresses": ["1.1.1.1", "1.0.0.1"],
    },
    "resolution_timeout_seconds": 5,
    "max_ttl_seconds": 60,
}


class _ScriptedStream(httpcore.NetworkStream):
    def __init__(self, peer=("93.184.216.34", 443), body=b"hello"):
        self.peer = peer
        self.buffer = bytearray(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\n\r\n"
            + body
        )
        self.writes = bytearray()
        self.sni = []
        self.closed = False

    def read(self, max_bytes, timeout=None):
        chunk = bytes(self.buffer[:max_bytes])
        del self.buffer[:max_bytes]
        return chunk

    def write(self, buffer, timeout=None):
        self.writes.extend(buffer)

    def close(self):
        self.closed = True

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self.sni.append(server_hostname)
        return self

    def get_extra_info(self, info):
        if info == "server_addr":
            return self.peer
        if info == "ssl_object":
            return None
        if info == "is_readable":
            return bool(self.buffer)
        return None


class _RecordingBackend(httpcore.NetworkBackend):
    def __init__(self, stream):
        self.stream = stream
        self.calls = []

    def connect_tcp(
        self, host, port, timeout=None, local_address=None, socket_options=None
    ):
        self.calls.append((host, port, timeout, local_address))
        return self.stream

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise AssertionError("unix sockets are not permitted")


class _AddressEchoBackend(httpcore.NetworkBackend):
    def __init__(self):
        self.calls = []

    def connect_tcp(
        self, host, port, timeout=None, local_address=None, socket_options=None
    ):
        self.calls.append((host, port))
        return _ScriptedStream(peer=(host, port))

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise AssertionError("unix sockets are not permitted")


def _target(address="93.184.216.34", hostname="example.com", port=443):
    resolution = transport.PublicResolution(
        hostname=hostname,
        addresses=(address,),
        resolver="resolver.example",
        resolved_at=1,
        expires_at=2,
    )
    return transport._PinnedTarget(
        url=httpx.URL(f"https://{hostname}/page"),
        hostname=hostname,
        port=port,
        resolution=resolution,
    )


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.1.1",
        "198.18.0.1",
        "224.0.0.1",
        "0.0.0.0",
        "::1",
        "fc00::1",
        "fe80::1",
        "2001:db8::1",
    ],
)
def test_non_public_and_fake_ip_addresses_are_never_accepted(address):
    with pytest.raises(transport.PublicTransportError) as caught:
        transport._public_address(address)
    assert caught.value.reason == "address-not-public"


def test_config_requires_explicit_https_bootstrap_and_bounded_timeout():
    assert transport._settings_from_config(
        PUBLIC_CONFIG
    ).resolver_bootstrap_addresses == (
        "1.1.1.1",
        "1.0.0.1",
    )
    bad_configs = [
        {},
        {**PUBLIC_CONFIG, "enabled": False},
        {**PUBLIC_CONFIG, "resolver": {**PUBLIC_CONFIG["resolver"], "kind": "system"}},
        {
            **PUBLIC_CONFIG,
            "resolver": {
                **PUBLIC_CONFIG["resolver"],
                "endpoint": "http://cloudflare-dns.com/dns-query",
            },
        },
        {
            **PUBLIC_CONFIG,
            "resolver": {
                **PUBLIC_CONFIG["resolver"],
                "endpoint": "https://username:password@cloudflare-dns.com/dns-query",
            },
        },
        {
            **PUBLIC_CONFIG,
            "resolver": {
                **PUBLIC_CONFIG["resolver"],
                "bootstrap_addresses": ["1.1.1.1", "198.18.0.1"],
            },
        },
        {**PUBLIC_CONFIG, "resolution_timeout_seconds": 11},
        {**PUBLIC_CONFIG, "max_ttl_seconds": 1.5},
    ]
    for config in bad_configs:
        with pytest.raises(transport.PublicTransportError) as caught:
            transport.create_public_client_factory(config)
        assert caught.value.reason == "transport-config-invalid"


def test_doh_resolution_validates_every_a_and_aaaa_answer(monkeypatch):
    resolver = transport._DohResolver(transport._settings_from_config(PUBLIC_CONFIG))

    def answer(_hostname, record_type, _timeout):
        if record_type == "A":
            return {
                "Status": 0,
                "Question": [{"name": "example.com.", "type": 1}],
                "Answer": [
                    {
                        "name": "example.com.",
                        "type": 5,
                        "TTL": 120,
                        "data": "edge.example.",
                    },
                    {
                        "name": "edge.example.",
                        "type": 1,
                        "TTL": 90,
                        "data": "93.184.216.34",
                    },
                ],
            }
        return {
            "Status": 0,
            "Question": [{"name": "example.com.", "type": 28}],
            "Answer": [
                {
                    "name": "example.com.",
                    "type": 28,
                    "TTL": 45,
                    "data": "2606:2800:220:1:248:1893:25c8:1946",
                }
            ],
        }

    monkeypatch.setattr(resolver, "_query_json", answer)
    resolved = resolver.resolve("example.com")
    assert resolved.addresses == (
        "93.184.216.34",
        "2606:2800:220:1:248:1893:25c8:1946",
    )
    assert resolved.resolver == "cloudflare-dns.com"
    assert 44 <= resolved.expires_at - resolved.resolved_at <= 45

    def mixed_answer(_hostname, record_type, _timeout):
        data = "93.184.216.34" if record_type == "A" else "198.18.0.9"
        kind = 1 if record_type == "A" else 28
        return {
            "Status": 0,
            "Question": [{"name": "example.com.", "type": kind}],
            "Answer": [{"name": "example.com.", "type": kind, "TTL": 30, "data": data}],
        }

    monkeypatch.setattr(resolver, "_query_json", mixed_answer)
    with pytest.raises(transport.PublicTransportError) as caught:
        resolver.resolve("example.com")
    assert caught.value.reason == "address-not-public"


def test_pinned_backend_connects_to_verified_ip_and_keeps_host_and_sni():
    stream = _ScriptedStream()
    backend = _RecordingBackend(stream)
    pinned = transport._PinnedHTTPTransport(_target(), network_backend=backend)
    request = httpx.Request("GET", "https://example.com/page")
    response = pinned.handle_request(request)
    assert backend.calls[0][0:2] == ("93.184.216.34", 443)
    assert stream.sni == ["example.com"]
    assert b"Host: example.com" in stream.writes
    assert response.extensions["kin_pinned_peer"] == "93.184.216.34"
    assert b"".join(response.iter_bytes()) == b"hello"
    response.close()
    pinned.close()


def test_socket_peer_must_match_the_validated_snapshot():
    stream = _ScriptedStream(peer=("93.184.216.35", 443))
    backend = transport._PinnedNetworkBackend(_target(), _RecordingBackend(stream))
    with pytest.raises(transport.PublicTransportError) as caught:
        backend.connect_tcp("example.com", 443)
    assert caught.value.reason == "peer-address-mismatch"
    assert stream.closed is True


def test_factory_forbids_implicit_redirects_and_environment_proxy(monkeypatch):
    captured = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setenv("HTTPS_PROXY", "http://username:password@127.0.0.1:9")
    monkeypatch.setattr(transport.httpx, "Client", FakeClient)
    factory = transport.create_public_client_factory(PUBLIC_CONFIG)
    client = factory(timeout=4, follow_redirects=False)
    assert isinstance(client, FakeClient)
    assert captured["trust_env"] is False
    assert captured["follow_redirects"] is False
    assert isinstance(captured["transport"], transport.PublicHttpTransport)
    with pytest.raises(transport.PublicTransportError) as caught:
        factory(timeout=4, follow_redirects=True)
    assert caught.value.reason == "redirects-must-be-explicit"


def test_each_request_gets_a_fresh_resolution_but_one_request_keeps_its_snapshot(
    monkeypatch,
):
    public = transport.PublicHttpTransport.from_config(PUBLIC_CONFIG)
    resolutions = iter(("93.184.216.34", "93.184.216.35"))
    resolver_calls = []

    def resolve(hostname):
        resolver_calls.append(hostname)
        address = next(resolutions)
        return transport.PublicResolution(
            hostname=hostname,
            addresses=(address,),
            resolver="resolver.example",
            resolved_at=1,
            expires_at=2,
        )

    backend = _AddressEchoBackend()
    monkeypatch.setattr(public._resolver, "resolve", resolve)
    monkeypatch.setattr(transport.httpcore, "SyncBackend", lambda: backend)
    with httpx.Client(
        transport=public, timeout=2, follow_redirects=False, trust_env=False
    ) as client:
        peers = []
        for _ in range(2):
            with client.stream("GET", "https://example.com/page") as response:
                peers.append(
                    response.extensions["kin_public_transport"]["peer_address"]
                )
                assert b"".join(response.iter_bytes()) == b"hello"
    assert resolver_calls == ["example.com", "example.com"]
    assert backend.calls == [("93.184.216.34", 443), ("93.184.216.35", 443)]
    assert peers == ["93.184.216.34", "93.184.216.35"]


def test_public_transport_rejects_non_read_methods_before_network():
    public = transport.PublicHttpTransport.from_config(PUBLIC_CONFIG)
    with pytest.raises(transport.PublicTransportError) as caught:
        public.handle_request(httpx.Request("POST", "https://example.com/"))
    assert caught.value.reason == "method-not-read-only"
