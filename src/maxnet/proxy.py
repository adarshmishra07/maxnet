"""Local SOCKS5 + HTTP proxy on one port. Every new connection leaves via the pool's pick."""
import asyncio
import contextlib
import socket
from urllib.parse import urlsplit

from .net import open_via


async def _pipe(src, dst, pool, iface):
    try:
        while data := await src.read(65536):
            dst.write(data)
            pool.count(iface, len(data))
            await dst.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        with contextlib.suppress(RuntimeError):  # the event loop may already be closing at shutdown
            dst.close()


async def _relay(reader, writer, pool, host, port, first=b"", ok=b""):
    failed = set()
    while True:  # a network that just dropped or can't reach the host is skipped; the next one is tried
        try:
            iface = pool.pick(exclude=failed)
        except RuntimeError:
            writer.close()
            return False
        pool.opened(iface)  # reserve now: concurrent picks during the connect must see this slot as taken
        try:
            up_r, up_w = await open_via(iface, host, port)
            break
        except OSError:
            pool.closed(iface)
            pool.mark(iface.name, False)  # no working internet on it right now: stop sending it traffic until it passes a check
            failed.add(iface.name)
        except BaseException:  # bad hostname (UnicodeError), cancelled handler, ...: never leave the slot reserved
            pool.closed(iface)
            raise
    try:
        if ok:
            writer.write(ok)
        if first:
            up_w.write(first)
        await asyncio.gather(_pipe(reader, up_w, pool, iface), _pipe(up_r, writer, pool, iface))
    finally:
        pool.closed(iface)
        with contextlib.suppress(RuntimeError):
            up_w.close()
    return True


async def _socks5(reader, writer, pool):
    n = (await reader.readexactly(1))[0]
    await reader.readexactly(n)
    writer.write(b"\x05\x00")
    ver, cmd, _, atyp = await reader.readexactly(4)
    if cmd != 1:
        writer.write(b"\x05\x07\x00\x01" + bytes(6))
        return writer.close()
    if atyp == 1:
        host = socket.inet_ntoa(await reader.readexactly(4))
    elif atyp == 3:
        host = (await reader.readexactly((await reader.readexactly(1))[0])).decode()
    else:  # IPv6 targets unsupported (pinned sockets are IPv4)
        writer.write(b"\x05\x08\x00\x01" + bytes(6))
        return writer.close()
    port = int.from_bytes(await reader.readexactly(2), "big")
    await _relay(reader, writer, pool, host, port, ok=b"\x05\x00\x00\x01" + bytes(6))


async def _http(reader, writer, pool, first):
    head = first + await reader.readuntil(b"\r\n\r\n")
    line, _, rest = head.partition(b"\r\n")
    method, target, version = line.decode().split(" ", 2)
    if method == "CONNECT":
        host, _, port = target.rpartition(":")
        return await _relay(reader, writer, pool, host, int(port), ok=b"HTTP/1.1 200 Connection established\r\n\r\n")
    u = urlsplit(target)
    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
    fwd = f"{method} {path} {version}\r\n".encode() + rest
    await _relay(reader, writer, pool, u.hostname, u.port or 80, first=fwd)


def handler(pool, tasks):
    async def handle(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        try:
            first = await reader.readexactly(1)
            if first == b"\x05":
                await _socks5(reader, writer, pool)
            else:
                await _http(reader, writer, pool, first)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError, ValueError, RuntimeError, OSError):
            pass
        finally:
            with contextlib.suppress(RuntimeError):
                writer.close()
    return handle


async def serve(pool, host="127.0.0.1", port=1080):
    tasks = set()
    server = await asyncio.start_server(handler(pool, tasks), host, port)
    server.maxnet_tasks = tasks  # so shutdown() can finish the open connections
    return server


async def shutdown(server):
    """Stop listening and end every open connection cleanly, so quitting prints no 'Event loop is closed' errors."""
    server.close()
    tasks = list(getattr(server, "maxnet_tasks", ()))
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
