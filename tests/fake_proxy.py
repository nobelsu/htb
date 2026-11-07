"""A recording HTTP proxy for tests: absolute-form requests and CONNECT tunnels, with optional Basic auth."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
from urllib.parse import urlsplit


@dataclass
class Seen:
    method: str
    target: str  # absolute URL, or host:port for CONNECT
    username: str | None
    password: str | None


@dataclass
class FakeProxy:
    require_auth: bool = True
    seen: list[Seen] = field(default_factory=list)
    server: asyncio.base_events.Server | None = None
    port: int = 0

    async def start(self, host: str = "127.0.0.1") -> FakeProxy:
        self.server = await asyncio.start_server(self._handle, host, 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()

    def paths(self) -> list[str]:
        return [urlsplit(s.target).path if "://" in s.target else s.target for s in self.seen]

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, ConnectionError):
            writer.close()
            return
        lines = head[:-4].decode("latin-1").split("\r\n")
        method, target, _ = lines[0].split(" ", 2)
        headers = [ln.split(":", 1) for ln in lines[1:] if ":" in ln]
        auth = next((v.strip() for k, v in headers if k.lower() == "proxy-authorization"), None)
        user = password = None
        if auth and auth.lower().startswith("basic "):
            user, _, password = base64.b64decode(auth[6:]).decode().partition(":")
        self.seen.append(Seen(method, target, user, password))
        if self.require_auth and user is None:
            writer.write(b"HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic\r\n"
                         b"Content-Length: 0\r\nConnection: close\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        if method.upper() == "CONNECT":
            host, _, port = target.rpartition(":")
            up_reader, up_writer = await asyncio.open_connection(host.strip("[]"), int(port))
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
        else:
            url = urlsplit(target)
            up_reader, up_writer = await asyncio.open_connection(url.hostname, url.port or 80)
            path = url.path + (f"?{url.query}" if url.query else "")
            kept = [ln for ln in lines[1:] if not ln.lower().startswith(("proxy-", "connection:", "keep-alive:"))]
            up_writer.write(("\r\n".join([f"{method} {path or '/'} HTTP/1.1", *kept, "Connection: close"])
                             + "\r\n\r\n").encode("latin-1"))
            await up_writer.drain()
        await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
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
