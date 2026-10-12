"""Interfaces, the pool that picks one per connection, and sockets pinned to an interface."""
import asyncio
import contextlib
import fcntl
import json
import math
import os
import socket
import sys
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import psutil

CONFIG = Path(os.environ.get("MAXNET_CONFIG", Path.home() / ".config" / "maxnet" / "config.json"))
IP_BOUND_IF = 25  # macOS: force egress interface, no root needed
DEFAULT_PORT = int(os.environ.get("MAXNET_PORT", 1080))  # the local proxy port, shared by the TUI, the CLI and app launching


@dataclass(frozen=True)
class Iface:
    name: str
    ip: str


VIRTUAL = ("lo", "utun", "bridge", "awdl", "llw", "ap", "gif", "stf", "anpi", "tun", "tap", "docker", "br-", "veth", "vmnet")


def list_interfaces():
    """Up IPv4 interfaces that could carry internet traffic (no loopback / link-local)."""
    stats = psutil.net_if_stats()
    out = []
    for name, addrs in psutil.net_if_addrs().items():
        ip = next((a.address for a in addrs if a.family == socket.AF_INET), None)
        st = stats.get(name)  # an interface can vanish between the two snapshots (cable pulled)
        if not ip or st is None or ip.startswith(("127.", "169.254.")) or not st.isup or name.startswith(VIRTUAL):
            continue
        out.append(Iface(name, ip))
    return sorted(out, key=lambda i: i.name)


def load_config():
    try:
        return json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        return {}


@contextlib.contextmanager
def _locked():
    """One writer at a time across processes (the TUI and CLI commands share this file)."""
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG.parent / (CONFIG.name + ".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def update_config(**kw):
    """Read-modify-write under a lock, replacing the file atomically: it holds the only copy of the user's original proxy settings."""
    with _locked():
        tmp = CONFIG.with_name(CONFIG.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps({**load_config(), **kw}))
        os.replace(tmp, CONFIG)


def load_enabled():
    return set(load_config().get("enabled", []))


def save_enabled(names):
    update_config(enabled=sorted(names))


RATE_WINDOW = 2.0       # seconds: how far back "current speed" looks
PEAK_HALF_LIFE = 90.0   # seconds: how slowly a network's best observed speed fades
MIN_WEIGHT = 0.05       # a very slow network still gets an occasional connection, so it can prove it recovered
RAMP_TIME = 3.0         # seconds a brand-new connection counts as load while it ramps up (idle long-lived ones do not count)
RAMP_COST = 100_000     # bytes/s of expected load per ramping connection
PROBE_HOST = ("1.1.1.1", 443)  # a cheap connection test to see whether a network really has internet
STALE_AFTER = 20.0      # seconds without data on a network before what we learned about it is forgotten (it may have recovered)


class Pool:
    """Tracks enabled interfaces, live counters, and how fast each network really is."""

    def __init__(self, enabled=None, lister=list_interfaces):
        self.lister = lister
        cfg = load_config()
        if enabled is not None:
            self.enabled = set(enabled)
        elif "enabled" in cfg:
            self.enabled = set(cfg["enabled"])
        else:  # first run: start with every real network so it works out of the box
            self.enabled = {i.name for i in lister()}
        self.active = {}  # name -> open connections
        self.bytes = {}   # name -> total bytes moved
        self._ewma = {}   # name -> (decayed byte count, time)
        self._peak = {}   # name -> (best observed bytes/s, time)
        self._opens = {}  # name -> times of recent connection openings
        self.ready = {}   # name -> False while the network has no working internet (unknown counts as ready)
        self.wake = None  # set by the health loop so a failure is re-checked right away

    def live(self):
        return [i for i in self.lister() if i.name in self.enabled]

    def restricted(self, names):
        """A view that only uses `names` but shares this pool's counters. Never touches the real selection or the config file."""
        view = Pool(enabled=names, lister=self.lister)
        view.active, view.bytes, view._ewma, view._peak, view._opens, view.ready = self.active, self.bytes, self._ewma, self._peak, self._opens, self.ready
        return view

    def toggle(self, name):
        self.enabled = self.enabled ^ {name}
        save_enabled(self.enabled)

    def pick(self, exclude=()):
        """Best enabled network for a new connection, skipping the names in `exclude` (ones that just failed).
        Chooses the network with the least load for its measured capacity. Load is its current speed plus the connections
        it just started; idle long-lived connections (browser keep-alives) are not load and must not push traffic away."""
        live = [i for i in self.live() if i.name not in exclude]
        if not live:
            raise RuntimeError("no enabled network is available")
        live = [i for i in live if self.ready.get(i.name, True)] or live  # skip networks with no internet yet, unless that is all there is
        now = time.monotonic()
        return min(live, key=lambda i: ((self.rate(i.name, now) + RAMP_COST * self.ramping(i.name, now)) / self.weight(i.name),
                                        self.bytes.get(i.name, 0)))

    def mark(self, name, ok):
        """Record whether a network really has internet. A failure wakes the health loop so it is re-checked within seconds."""
        was = self.ready.get(name, True)
        self.ready[name] = ok
        if was and not ok and self.wake is not None:
            self.wake.set()

    def waiting(self):
        """Selected networks that are plugged in but do not have working internet yet."""
        return [i for i in self.live() if not self.ready.get(i.name, True)]

    def ramping(self, name, now=None):
        """Connections opened in the last few seconds: they have not reached full speed, so the speed reading does not show them yet."""
        now = time.monotonic() if now is None else now
        q = self._opens.setdefault(name, deque())
        while q and now - q[0] > RAMP_TIME:
            q.popleft()
        return len(q)

    def rate(self, name, now=None):
        """Current speed of a network in bytes/s (recent average)."""
        v, t = self._ewma.get(name, (0.0, 0.0))
        now = time.monotonic() if now is None else now
        return v * math.exp(-(now - t) / RATE_WINDOW) / RATE_WINDOW

    def peak(self, name, now=None):
        """Best speed seen on this network lately, in bytes/s."""
        pk, t = self._peak.get(name, (0.0, 0.0))
        now = time.monotonic() if now is None else now
        return pk * 0.5 ** ((now - t) / PEAK_HALF_LIFE)

    def weight(self, name):
        """Relative capacity 0.05..1 of a network. Unknown or stale networks count as the best one, so every network gets a chance
        and a network that was slow once cannot be locked out forever (it gets no traffic, so it could never be re-measured)."""
        now = time.monotonic()
        fresh = {n: self.peak(n, now) for n, (_, t) in self._peak.items() if now - t <= STALE_AFTER}
        best = max(fresh.values(), default=0.0)
        mine = fresh.get(name, 0.0)
        if best <= 0 or mine <= 0:
            return 1.0
        return max(mine / best, MIN_WEIGHT)

    def opened(self, iface):
        self.active[iface.name] = self.active.get(iface.name, 0) + 1
        self._opens.setdefault(iface.name, deque()).append(time.monotonic())

    def closed(self, iface):
        self.active[iface.name] = max(0, self.active.get(iface.name, 0) - 1)

    def count(self, iface, n):
        name, now = iface.name, time.monotonic()
        self.bytes[name] = self.bytes.get(name, 0) + n
        v, t = self._ewma.get(name, (0.0, now))
        self._ewma[name] = (v * math.exp(-(now - t) / RATE_WINDOW) + n, now)
        self._peak[name] = (max(self.peak(name, now), self.rate(name, now)), now)


def _pin(sock, iface):
    if sys.platform == "darwin":
        sock.setsockopt(socket.IPPROTO_IP, IP_BOUND_IF, socket.if_nametoindex(iface.name))
    elif sys.platform.startswith("linux"):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, iface.name.encode())
        except PermissionError:
            pass  # older kernels need CAP_NET_RAW; source-IP bind below still applies
    sock.bind((iface.ip, 0))


async def open_via(iface, host, port, ssl=None, timeout=10.0):
    """TCP connection that leaves through `iface` (IPv4 only). Fails fast (ConnectionError) if the network can't reach the host."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, family=socket.AF_INET, type=socket.SOCK_STREAM)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setblocking(False)
        _pin(sock, iface)
        try:
            await asyncio.wait_for(loop.sock_connect(sock, infos[0][4]), timeout)
        except asyncio.TimeoutError:
            raise ConnectionError(f"{iface.name} could not reach {host}:{port} within {timeout:.0f}s") from None
        return await asyncio.open_connection(sock=sock, ssl=ssl, server_hostname=host if ssl else None)
    except BaseException:
        sock.close()
        raise


async def health_loop(pool, every=20.0, retry=3.0, timeout=3.0):
    """Keep Pool.ready up to date: re-check often while a network is not ready (so it joins by itself the moment it works),
    rarely once everything is fine."""
    pool.wake = asyncio.Event()

    async def one(iface):
        try:
            _, w = await open_via(iface, *PROBE_HOST, timeout=timeout)
            w.close()
            pool.mark(iface.name, True)
        except OSError:
            pool.mark(iface.name, False)

    while True:
        await asyncio.gather(*(one(i) for i in pool.live()))
        try:
            await asyncio.wait_for(pool.wake.wait(), retry if pool.waiting() else every)
        except asyncio.TimeoutError:
            pass
        pool.wake.clear()
