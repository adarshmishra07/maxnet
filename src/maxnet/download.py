"""Plexo-style downloader: one file, split into Range blocks, fetched over every enabled interface."""
import asyncio
import os
import ssl as ssllib
from urllib.parse import unquote, urljoin, urlsplit

import truststore  # OS trust store, so corporate/WARP-style certificates validate like in curl

from .net import open_via

BLOCK = 8 * 1024 * 1024      # largest block
MIN_BLOCK = 1024 * 1024      # smallest block; small files get small blocks so every stream has work
STREAMS_PER_IFACE = 16
STALL = 15.0  # seconds without any data before a stream gives up and its block goes to another network


def safe_filename(url):
    """File name from a URL, never a path: unquote first, then keep only the last component."""
    name = os.path.basename(unquote(urlsplit(url).path).replace("\\", "/")).strip()
    return name if name not in ("", ".", "..") else "download"


async def _connect(iface, url, timeout=10.0):
    u = urlsplit(url)
    tls = u.scheme == "https"
    return await open_via(iface, u.hostname, u.port or (443 if tls else 80),
                          ssl=truststore.SSLContext(ssllib.PROTOCOL_TLS_CLIENT) if tls else None, timeout=timeout)


async def _send(reader, writer, url, headers, keepalive=False):
    """Send one GET on an open connection and read the response head. Returns (status, headers)."""
    u = urlsplit(url)
    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
    lines = [f"GET {path} HTTP/1.1", f"Host: {u.netloc}", f"Connection: {'keep-alive' if keepalive else 'close'}",
             "Accept-Encoding: identity", "User-Agent: maxnet/0.1"]
    lines += [f"{k}: {v}" for k, v in headers.items()]
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
    head = (await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), STALL)).decode("latin-1").split("\r\n")
    status = int(head[0].split()[1])
    hdrs = {k.lower(): v.strip() for k, _, v in (h.partition(":") for h in head[1:] if h)}
    return status, hdrs


async def request(iface, url, headers, timeout=10.0):
    """One-shot HTTP/1.1 GET pinned to `iface`. Returns (status, headers, reader, writer)."""
    reader, writer = await _connect(iface, url, timeout)
    status, hdrs = await _send(reader, writer, url, headers)
    return status, hdrs, reader, writer


async def probe(iface, url, timeout=5.0):
    """Follow redirects; return (final_url, total_size or None if ranges unsupported)."""
    for _ in range(8):
        status, h, _, w = await request(iface, url, {"Range": "bytes=0-0"}, timeout)
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


class _Slot:
    """One stream's connection, kept open between blocks so each block skips the TCP and TLS handshakes."""

    def __init__(self):
        self.conn = None

    def drop(self):
        if self.conn:
            self.conn[1].close()
            self.conn = None


async def _fetch_block(iface, url, fd, start, end, pool, progress, slot):
    want = end - start + 1
    for attempt in (0, 1):
        reused = slot.conn is not None
        try:
            if slot.conn is None:
                slot.conn = await _connect(iface, url)
            reader, writer = slot.conn
            status, h = await _send(reader, writer, url, {"Range": f"bytes={start}-{end}"}, keepalive=True)
            break
        except (OSError, asyncio.IncompleteReadError, asyncio.TimeoutError):
            slot.drop()
            if reused and attempt == 0:
                continue  # the server closed our idle connection: retry once on a fresh one
            raise
    try:
        if status != 206:
            raise RuntimeError(f"expected 206, got {status}")
        pos = start
        while pos <= end:
            data = await asyncio.wait_for(reader.read(min(65536, end - pos + 1)), STALL)
            if not data:
                raise ConnectionError("short block")
            os.pwrite(fd, data, pos)
            pos += len(data)
            pool.count(iface, len(data))
            progress(len(data))
        if h.get("content-length") != str(want) or "close" in h.get("connection", "").lower():
            slot.drop()  # the server will not keep it open (or the framing is unclear)
    except BaseException:
        slot.drop()
        raise


MAX_FAILS = 3   # a network that fails this many blocks in a row is retired; the other networks finish the job
SLOW_AFTER = 2.0       # seconds before judging speeds (streams need time to ramp up)
SLOW_RATIO = 0.10      # a network below 10% of the best one's speed is retired for this download
SLOW_MIN_BEST = 1_000_000  # bytes/s (8 Mbit/s): below this everything is slow, so there is nothing to compare


async def _probe_all(ifaces, url):
    """Probe every network at once. Returns (final_url, size, networks that can reach the server)."""
    results = await asyncio.gather(*(probe(i, url) for i in ifaces), return_exceptions=True)
    good = [(i, r) for i, r in zip(ifaces, results) if not isinstance(r, BaseException)]
    if not good:
        raise RuntimeError(f"no network could reach the server: {results[0]}")
    return good[0][1][0], good[0][1][1], [i for i, _ in good]


async def download(url, out, pool, progress=lambda n: None, block=None, streams=STREAMS_PER_IFACE, note=lambda msg: None):
    """Returns total bytes. Needs a server that supports Range requests."""
    ifaces = pool.live()
    if not ifaces:
        raise RuntimeError("no enabled interfaces: run `maxnet add <name>`")
    url, total, reachable = await _probe_all(ifaces, url)
    for i in ifaces:
        if i not in reachable:
            note(f"{i.name}: cannot reach the server, skipped")
    ifaces = reachable
    if total is None:
        raise RuntimeError("server does not support Range requests; use curl via the proxy instead")
    part = out + ".part"
    fd = os.open(part, os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW)  # never write through a symlink
    try:
        queue, inflight = asyncio.Queue(), [0]
        os.ftruncate(fd, total)
        block = block or max(MIN_BLOCK, min(BLOCK, total // (len(ifaces) * streams * 2)))
        for b in split_blocks(total, block):
            queue.put_nowait(b)

        fails = {i.name: 0 for i in ifaces}  # consecutive failures per NETWORK, shared by all its streams
        tasks = {i.name: [] for i in ifaces}

        async def worker(iface):
            slot = _Slot()
            try:
                await _work(iface, slot)
            finally:
                slot.drop()

        async def _work(iface, slot):
            while fails[iface.name] < MAX_FAILS:  # a network that keeps failing is retired as a whole
                try:
                    start, end = queue.get_nowait()
                except asyncio.QueueEmpty:
                    if not inflight[0]:
                        return
                    await asyncio.sleep(0.05)  # a block in flight may still fail and come back
                    continue
                inflight[0] += 1
                pool.opened(iface)
                try:
                    await _fetch_block(iface, url, fd, start, end, pool, progress, slot)
                    fails[iface.name] = 0
                except asyncio.CancelledError:
                    queue.put_nowait((start, end))  # this network was retired as too slow: the others take the block
                    raise
                except (OSError, RuntimeError, asyncio.IncompleteReadError, asyncio.TimeoutError):
                    fails[iface.name] += 1
                    queue.put_nowait((start, end))  # goes back for the other networks
                    await asyncio.sleep(0.05)       # let a healthy stream grab it first
                finally:
                    pool.closed(iface)
                    inflight[0] -= 1

        async def monitor():
            """A network far slower than the best one (throttled, weak signal) only holds blocks back: retire it."""
            await asyncio.sleep(SLOW_AFTER)
            while queue.qsize() or inflight[0]:
                rates = {i.name: pool.rate(i.name) for i in ifaces if any(not t.done() for t in tasks[i.name])}
                best = max(rates.values(), default=0.0)
                if best >= SLOW_MIN_BEST:
                    for name, r in rates.items():
                        if r < SLOW_RATIO * best:
                            note(f"{name}: much slower than the others ({r * 8 / 1e6:.0f} Mbit/s), stopped using it for this download")
                            for t in tasks[name]:
                                t.cancel()
                await asyncio.sleep(1.0)

        # round-robin across networks, so the first blocks are spread out instead of all taken by the first network
        workers = []
        for _ in range(streams):
            for i in ifaces:
                t = asyncio.ensure_future(worker(i))
                tasks[i.name].append(t)
                workers.append(t)
        mon = asyncio.ensure_future(monitor())
        try:
            results = await asyncio.gather(*workers, return_exceptions=True)
        finally:
            mon.cancel()
        errors = [r for r in results if isinstance(r, Exception)]
        if errors:
            raise errors[0]
        if not queue.empty():
            raise RuntimeError("download incomplete: every network gave up on the remaining blocks")
    finally:
        os.close(fd)
    os.replace(part, out)
    return total
