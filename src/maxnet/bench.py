"""Speed test: each enabled network alone, then all together, using the real range downloader."""
import asyncio
import contextlib
import shutil
import tempfile
import time

from .download import download

URL = "https://releases.ubuntu.com/24.04/ubuntu-24.04.3-live-server-amd64.iso"
WARMUP = 2.0  # seconds ignored while streams ramp up


def bar(frac, width=36):
    n = round(min(max(frac, 0), 1) * width)
    return "━" * n + "─" * (width - n)


async def measure(pool, names, url=URL, secs=10.0):
    """Mbit/s per network while only `names` are enabled. Restores the pool afterwards."""
    saved = pool.enabled
    pool.enabled = set(names)
    tmp = tempfile.mkdtemp(prefix="maxnet-bench-")  # private 0700 dir, unpredictable name
    out = f"{tmp}/bench.bin"
    task = asyncio.create_task(download(url, out, pool))
    try:
        await asyncio.wait({task}, timeout=WARMUP)
        before = {n: pool.bytes.get(n, 0) for n in names}
        t0 = time.monotonic()
        await asyncio.wait({task}, timeout=max(secs - WARMUP, 1))
        if task.done():
            task.result()  # surfaces a failed download (403, no Range, ...)
        dt = time.monotonic() - t0
        return {n: (pool.bytes.get(n, 0) - before[n]) * 8 / dt / 1e6 for n in names}
    finally:
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        pool.enabled = saved
        shutil.rmtree(tmp, ignore_errors=True)


async def run_all(pool, report, url=URL, secs=10.0):
    """Calls report(names, {name: mbit}) after each step: every network alone, then combined."""
    names = sorted(i.name for i in pool.live())
    steps = [[n] for n in names] + ([names] if len(names) > 1 else [])
    for combo in steps:
        report(combo, await measure(pool, combo, url, secs))
