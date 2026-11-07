"""Local proxy run inside the Neko container: Chromium can't send proxy credentials itself, so it uses this
(--proxy-server=http://127.0.0.1:PORT), which adds the residential proxy's credentials and passes everything on.
Standard library only (it runs on the image's python3).

Usage: proxy_auth.py LISTEN_PORT UPSTREAM_FILE
UPSTREAM_FILE is JSON {"host", "port", "username", "password"} (an http:// proxy). It is read once at start.
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys

HEADER_LIMIT = 65536
HOP_HEADERS = (b"proxy-authorization:", b"proxy-connection:", b"connection:", b"keep-alive:")


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        try:
            writer.close()
        except (ConnectionError, OSError):
            pass


async def read_head(reader: asyncio.StreamReader) -> bytes:
    """Request or response line plus headers, through the blank line."""
    return await reader.readuntil(b"\r\n\r\n")


def with_auth(head: bytes, auth: bytes, close: bool) -> bytes:
    """The client's request head with our Proxy-Authorization (and, for plain http, Connection: close, so the next
    request comes on a new connection, which gets credentials too)."""
    lines = head[:-4].split(b"\r\n")
    kept = [lines[0]] + [ln for ln in lines[1:] if not ln.lower().startswith(HOP_HEADERS)]
    kept.append(b"Proxy-Authorization: " + auth)
    if close:
        kept.append(b"Connection: close")
    return b"\r\n".join(kept) + b"\r\n\r\n"


def handler(host: str, port: int, auth: bytes):  # type: ignore[no-untyped-def]
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await read_head(reader)
            up_reader, up_writer = await asyncio.open_connection(host, port)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, OSError):
            writer.close()
            return
        tunnel = head.split(b" ", 1)[0].upper() == b"CONNECT"
        up_writer.write(with_auth(head, auth, close=not tunnel))
        await up_writer.drain()
        await asyncio.gather(pipe(reader, up_writer), pipe(up_reader, writer))

    return handle


async def serve(listen_port: int, upstream: dict[str, object], host: str = "127.0.0.1") -> asyncio.base_events.Server:
    user, password = str(upstream.get("username", "")), str(upstream.get("password", ""))
    auth = b"Basic " + base64.b64encode(f"{user}:{password}".encode())
    return await asyncio.start_server(
        handler(str(upstream["host"]), int(str(upstream["port"])), auth), host, listen_port, limit=HEADER_LIMIT
    )


async def main(listen_port: int, upstream_file: str) -> None:
    with open(upstream_file) as f:
        upstream = json.load(f)
    server = await serve(listen_port, upstream)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]), sys.argv[2]))
