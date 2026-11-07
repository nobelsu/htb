"""TCP forwarder run inside the Neko container (python3 is in the image; socat is not).

Usage: forward.py LISTEN_HOST:LISTEN_PORT TARGET_HOST:TARGET_PORT [LISTEN:TARGET ...]
Used to expose Chrome's DevTools port (bound to 127.0.0.1 only) on the container's network, and, in local
development, to make the container's 127.0.0.1:<port> reach the Docker host.
"""

import asyncio
import sys


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        writer.close()


def handler(target_host: str, target_port: int):  # type: ignore[no-untyped-def]
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            up_reader, up_writer = await asyncio.open_connection(target_host, target_port)
        except OSError:
            writer.close()
            return
        await asyncio.gather(pipe(reader, up_writer), pipe(up_reader, writer))

    return handle


async def main(specs: list[str]) -> None:
    servers = []
    for spec in specs:
        listen, target = spec.split("=")
        lhost, lport = listen.rsplit(":", 1)
        thost, tport = target.rsplit(":", 1)
        servers.append(await asyncio.start_server(handler(thost, int(tport)), lhost, int(lport)))
    await asyncio.gather(*(s.serve_forever() for s in servers))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
