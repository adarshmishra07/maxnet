"""Plexo-style downloader: one file, split into Range blocks, fetched over every enabled interface."""
import asyncio
import os
import ssl as ssllib
from urllib.parse import urljoin, urlsplit

import truststore  # OS trust store, so corporate/WARP-style certificates validate like in curl

from .net import open_via

BLOCK = 8 * 1024 * 1024
STREAMS_PER_IFACE = 16


async def request(iface, url, headers):
    """Minimal HTTP/1.1 GET pinned to `iface`. Returns (status, headers, reader, writer)."""
    u = urlsplit(url)
    tls = u.scheme == "https"
    reader, writer = await open_via(iface, u.hostname, u.port or (443 if tls else 80),
                                    ssl=truststore.SSLContext(ssllib.PROTOCOL_TLS_CLIENT) if tls else None)
    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
    lines = [f"GET {path} HTTP/1.1", f"Host: {u.netloc}", "Connection: close", "Accept-Encoding: identity", "User-Agent: maxnet/0.1"]
    lines += [f"{k}: {v}" for k, v in headers.items()]
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
    head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1").split("\r\n")
    status = int(head[0].split()[1])
    hdrs = {k.lower(): v.strip() for k, _, v in (h.partition(":") for h in head[1:] if h)}
    return status, hdrs, reader, writer


async def probe(iface, url):
    """Follow redirects; return (final_url, total_size or None if ranges unsupported)."""
    for _ in range(8):
        status, h, _, w = await request(iface, url, {"Range": "bytes=0-0"})
        w.close()
        if status in (301, 302, 303, 307, 308):
            url = urljoin(url, h["location"])
        elif status == 206:
            return url, int(h["content-range"].rpartition("/")[2])
        elif status == 200:
            return url, None
        else:
            raise RuntimeError(f"server answered {status}")
    raise RuntimeError("too many redirects")


def split_blocks(total, block=BLOCK):
    return [(s, min(s + block, total) - 1) for s in range(0, total, block)]


async def _fetch_block(iface, url, fd, start, end, pool, progress):
    status, h, reader, writer = await request(iface, url, {"Range": f"bytes={start}-{end}"})
    try:
        if status != 206:
            raise RuntimeError(f"expected 206, got {status}")
        pos = start
        while pos <= end:
            data = await reader.read(min(65536, end - pos + 1))
            if not data:
                raise ConnectionError("short block")
            os.pwrite(fd, data, pos)
            pos += len(data)
            pool.count(iface, len(data))
            progress(len(data))
    finally:
        writer.close()


async def download(url, out, pool, progress=lambda n: None, block=BLOCK, streams=STREAMS_PER_IFACE):
    """Returns total bytes. Single-stream fallback when the server ignores Range."""
    ifaces = pool.live()
    if not ifaces:
        raise RuntimeError("no enabled interfaces: run `maxnet add <name>`")
    url, total = await probe(ifaces[0], url)
    part = out + ".part"
    fd = os.open(part, os.O_RDWR | os.O_CREAT | os.O_TRUNC)
    try:
        queue = asyncio.Queue()
        if total is None:
            raise RuntimeError("server does not support Range requests; use curl via the proxy instead")
        os.ftruncate(fd, total)
        for b in split_blocks(total, block):
            queue.put_nowait((b, 0))

        async def worker(iface):
            while True:
                try:
                    (start, end), tries = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                pool.opened(iface)
                try:
                    await _fetch_block(iface, url, fd, start, end, pool, progress)
                except (OSError, RuntimeError, asyncio.IncompleteReadError):
                    if tries >= 5:
                        raise
                    queue.put_nowait(((start, end), tries + 1))  # another stream/network retries it
                finally:
                    pool.closed(iface)

        await asyncio.gather(*(worker(i) for i in ifaces for _ in range(streams)))
    finally:
        os.close(fd)
    os.replace(part, out)
    return total
