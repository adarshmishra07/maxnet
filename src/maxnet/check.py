"""One-key self-test: real requests through the real proxy, then which networks carried them."""
import asyncio

from .meta import probe_all


async def _via_proxy(port, host="example.com"):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(f"GET http://{host}/ HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
    body = await asyncio.wait_for(r.read(), 15)
    w.close()
    return b"Example Domain" in body


async def run_checks(pool, port, system_on, expect_system):
    """Returns a list of (ok, text)."""
    out = []
    live = pool.live()
    out.append((bool(live), f"{len(live)} network(s) selected"))
    metas = await probe_all(live)
    for i in live:
        m = metas[i.name]
        out.append((bool(m.reachable), f"{m.label or i.name}: " + (f"internet OK, ping {m.latency_ms:.0f} ms" if m.reachable else "NO INTERNET")))
    before = dict(pool.bytes)
    try:
        ok = await _via_proxy(port)
    except (OSError, asyncio.TimeoutError):
        ok = False
    out.append((ok, "a real request through the maxnet proxy " + ("worked" if ok else "FAILED")))
    used = [n for n in pool.bytes if pool.bytes[n] > before.get(n, 0)]
    out.append((bool(used), "it left through: " + (", ".join(sorted(used)) or "none")))
    if expect_system is not None:
        out.append((system_on == expect_system, "system proxy is " + ("ON" if system_on else "OFF") + ("" if system_on == expect_system else " (not what was expected)")))
    return out
