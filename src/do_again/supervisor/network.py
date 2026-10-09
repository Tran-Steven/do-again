"""Bounded trusted HTTPS; no redirects, proxies or script-selected origins."""
from __future__ import annotations

import http.client
import ssl
import time
from urllib.parse import urlsplit

from .macos_execution import INSTALL_ROOT, ExecutionBlocked, private_root_file


def trusted_context():
    # Never use SSL_CERT_FILE/DIR, host site packages or relocated interpreter
    # defaults to choose supervisor trust. The OS roots are sealed with the build.
    path = INSTALL_ROOT / 'current/runtimes/trust/ca.pem'
    private_root_file(path)
    return ssl.create_default_context(cafile=str(path))


def https_bytes(url: str, *, host: str, limit: int, method: str = 'GET',
                body: bytes | None = None, headers: dict | None = None,
                allow_query: bool = False) -> tuple[int, bytes]:
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname != host or parsed.username or parsed.password
            or parsed.port not in (None, 443) or parsed.fragment or (parsed.query and not allow_query)
            or not parsed.path.startswith('/') or '\\' in parsed.path
            or any(ord(c) < 32 for c in url)):
        raise ExecutionBlocked('network origin is not admitted')
    deadline = time.monotonic() + 30
    connection = http.client.HTTPSConnection(host, timeout=10, context=trusted_context())
    try:
        connection.request(method, parsed.path, body=body, headers=headers or {})
        response = connection.getresponse()
        # Never follow a redirect, including one on the original approved host.
        if 300 <= response.status < 400:
            raise ExecutionBlocked('network redirect is not admitted')
        kept = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ExecutionBlocked('network capability timed out')
            if connection.sock is not None:
                connection.sock.settimeout(min(10, remaining))
            part = response.read(min(65536, limit + 1 - len(kept)))
            if not part:
                return response.status, bytes(kept)
            kept.extend(part)
            if len(kept) > limit:
                raise ExecutionBlocked('network response exceeds capability budget')
    except (OSError, http.client.HTTPException) as exc:
        # Response bodies and credential headers must never reach receipts/logs.
        raise ExecutionBlocked('network capability failed; inspect durable effect state') from None
    finally:
        connection.close()
