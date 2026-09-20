# Public HTTP transport under FakeIP DNS

## Why this transport exists

In FakeIP/TUN mode, an operating-system lookup can deliberately return a
synthetic address such as `198.18.0.x`. The TUN may also capture packets sent to
a nominal public DNS server, so merely naming a different resolver or disabling
`httpx` environment trust does not prove that the returned address is public.

This transport does not allow the FakeIP range. It pins the TLS connection for
the configured DNS-over-HTTPS hostname to explicitly configured public
bootstrap addresses, requests A and AAAA records, rejects non-public answers,
then pins the target connection to that verified snapshot. The original target
hostname remains the HTTP Host and TLS SNI, and the application-visible socket
peer must match the selected address.

The implementation uses Cloudflare's documented JSON DoH endpoint and media
type. JSON DoH is provider-specific rather than an IETF-standard schema, so
unexpected schema changes fail closed instead of falling back to system DNS.

- Cloudflare DoH endpoint: <https://developers.cloudflare.com/1.1.1.1/encryption/dns-over-https/make-api-requests/>
- Cloudflare JSON response fields: <https://developers.cloudflare.com/1.1.1.1/encryption/dns-over-https/make-api-requests/dns-json/>
- HTTPX custom transports: <https://www.python-httpx.org/advanced/transports/>
- HTTPX environment controls: <https://www.python-httpx.org/environment_variables/>

## Configuration

The feature is opt-in. The host passes this object as
`exploration_web.public_transport` and copies it into `web-reader.json`:

```json
{
  "enabled": true,
  "resolver": {
    "kind": "doh-json",
    "endpoint": "https://cloudflare-dns.com/dns-query",
    "bootstrap_addresses": ["1.1.1.1", "1.0.0.1"]
  },
  "resolution_timeout_seconds": 5.0,
  "max_ttl_seconds": 60
}
```

The resolver endpoint must use HTTPS without credentials, query parameters, a
fragment, or a non-standard port. Every bootstrap address must be a public IP.
The resolver timeout is limited to ten seconds and the TTL cap to five minutes.
Invalid enabled configuration fails closed.

When the object is absent or `enabled` is false, `web_read.py` retains its
existing direct path for compatibility. That path must use `trust_env=False`
and keeps the existing public-address check, so FakeIP DNS remains refused. It
does not silently switch to this transport, a proxy, or a browser.

## Reader integration

```python
from kin_mind.http_transport import create_public_client_factory

client_factory = create_public_client_factory(config["public_transport"])
with client_factory(timeout=15, follow_redirects=False) as client:
    with client.stream("GET", url, headers=headers) as response:
        for chunk in response.iter_bytes():
            # The reader applies its own response-size ceiling here.
            consume_bounded(chunk)
```

The response remains streaming; the transport does not read the body. Each
request performs a fresh resolution and creates a one-request connection pool.
`web_read.py` must continue to handle redirects itself. Each accepted redirect
therefore produces another resolution, address-policy check, pinned TCP
connection, TLS check, and peer check.

`response.extensions["kin_public_transport"]` contains only non-secret
transport evidence: hostname, verified address set, actual peer address,
resolver hostname, resolution time, and expiry time.

## Security properties

- `198.18.0.0/15`, private, loopback, link-local, reserved, multicast, and
  unspecified addresses are refused. There is no range allowlist.
- All A and AAAA answers are examined. One non-public answer rejects the whole
  resolution; it is never discarded in favor of a public sibling answer.
- The original URL hostname remains the HTTP Host and TLS SNI. Certificate
  validation remains enabled while TCP connects only to an address from the
  verified snapshot.
- The socket peer must match both the selected address and port before a
  response is returned.
- Resolver bootstrap addresses come only from explicit configuration and are
  themselves subject to the same public-address policy.
- Clients are created with an explicit transport, `trust_env=False`, and
  `follow_redirects=False`. Only GET and HEAD are accepted.
- Resolver or target failures do not fall back to operating-system DNS,
  implicit proxy settings, FakeIP acceptance, or browser automation.
