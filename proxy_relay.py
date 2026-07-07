"""
Local authenticating proxy relay.

Why this exists: SeleniumBase feeds authenticated-proxy credentials through a
Manifest V3 background *service worker* (see seleniumbase proxy_helper.py). In
UC mode, undetected-chromedriver's disconnect/reconnect races that service
worker, so it isn't alive when the first request needs auth — Chrome then shows
the native OS proxy-auth popup and the page hangs/renders blank.

This relay sidesteps that entirely: it listens on 127.0.0.1 as an
*unauthenticated* HTTP proxy and forwards every connection to the upstream
DataImpulse gateway, injecting the `Proxy-Authorization` header ourselves
(exactly what `curl -x http://user:pass@host:port` does). Chrome is pointed at
127.0.0.1 with no auth, so there's no popup and nothing for UC mode to break.

Handles both HTTPS (CONNECT tunnels — all our target sites) and plain HTTP by
relaying the client's request line + headers upstream with auth added.

Reads credentials from env: PROXY_USERNAME, PROXY_PASSWORD, and optionally
PROXY_HOST (default gw.dataimpulse.com) / PROXY_PORT (default 823).
"""

import asyncio
import base64
import os
import threading
from contextlib import suppress


def _build_auth_header():
    """Return base64 'user:pass' for Proxy-Authorization, or None if unset."""
    user = os.environ.get("PROXY_USERNAME")
    password = os.environ.get("PROXY_PASSWORD")
    if not user or not password:
        return None
    return base64.b64encode(f"{user}:{password}".encode()).decode()


async def _pipe(reader, writer):
    """Copy bytes one direction until EOF, then close the writer."""
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        with suppress(Exception):
            writer.close()


def _make_handler(up_host, up_port, auth_b64):
    async def handle(client_reader, client_writer):
        upstream_writer = None
        try:
            # Read the client's request head (request line + headers).
            header = b""
            while b"\r\n\r\n" not in header:
                chunk = await client_reader.read(4096)
                if not chunk:
                    return
                header += chunk
                if len(header) > 65536:  # guard against a malformed flood
                    return

            head, _, rest = header.partition(b"\r\n\r\n")
            lines = head.split(b"\r\n")

            # Open a fresh connection to the upstream proxy for this client.
            upstream_reader, upstream_writer = await asyncio.open_connection(
                up_host, up_port
            )

            # Forward the request line unchanged (CONNECT host:443 / GET http://…),
            # strip any client-sent proxy-auth, and inject our own.
            new_lines = [lines[0]]
            for line in lines[1:]:
                if line.lower().startswith(b"proxy-authorization:"):
                    continue
                new_lines.append(line)
            new_lines.append(b"Proxy-Authorization: Basic " + auth_b64.encode())

            upstream_writer.write(b"\r\n".join(new_lines) + b"\r\n\r\n" + rest)
            await upstream_writer.drain()

            # Tunnel bytes both ways until either side closes.
            await asyncio.gather(
                _pipe(client_reader, upstream_writer),
                _pipe(upstream_reader, client_writer),
            )
        except Exception:
            pass
        finally:
            with suppress(Exception):
                client_writer.close()
            if upstream_writer is not None:
                with suppress(Exception):
                    upstream_writer.close()

    return handle


def start_proxy_relay(host="127.0.0.1", port=8899):
    """
    Start the relay in a daemon thread and return the local proxy address
    ("127.0.0.1:8899") to pass as SeleniumBase's `proxy=`. Returns None if the
    upstream credentials aren't configured (caller then runs without a proxy).
    """
    auth_b64 = _build_auth_header()
    if not auth_b64:
        return None

    up_host = os.environ.get("PROXY_HOST", "gw.dataimpulse.com")
    up_port = int(os.environ.get("PROXY_PORT", "823"))

    ready = threading.Event()
    startup_error = {}

    def run():
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            handler = _make_handler(up_host, up_port, auth_b64)
            loop.run_until_complete(asyncio.start_server(handler, host, port))
        except Exception as e:  # e.g. port already in use
            startup_error["err"] = e
            ready.set()
            return
        ready.set()
        loop.run_forever()

    thread = threading.Thread(target=run, daemon=True, name="proxy-relay")
    thread.start()

    if not ready.wait(timeout=5):
        raise RuntimeError("Proxy relay failed to start within 5s")
    if "err" in startup_error:
        raise startup_error["err"]

    return f"{host}:{port}"
