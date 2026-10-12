"""Interfaces, the pool that picks one per connection, and sockets pinned to an interface."""
import asyncio
import json
import math
import os
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import psutil

CONFIG = Path(os.environ.get("MAXNET_CONFIG", Path.home() / ".config" / "maxnet" / "config.json"))
IP_BOUND_IF = 25  # macOS: force egress interface, no root needed


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
        if not ip or ip.startswith(("127.", "169.254.")) or not stats[name].isup or name.startswith(VIRTUAL):
            continue
        out.append(Iface(name, ip))
    return sorted(out, key=lambda i: i.name)


def load_config():
    try:
        return json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        return {}


def update_config(**kw):
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps({**load_config(), **kw}))


def load_enabled():
    return set(load_config().get("enabled", []))


def save_enabled(names):
    update_config(enabled=sorted(names))


RATE_WINDOW = 2.0       # seconds: how far back "current speed" looks
PEAK_HALF_LIFE = 90.0   # seconds: how slowly a network's best observed speed fades
MIN_WEIGHT = 0.05       # a very slow network still gets an occasional connection, so it can prove it recovered


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

    def live(self):
        return [i for i in self.lister() if i.name in self.enabled]

    def toggle(self, name):
        self.enabled = self.enabled ^ {name}
        save_enabled(self.enabled)

    def pick(self, exclude=()):
        """Best enabled network for a new connection, skipping the names in `exclude` (ones that just failed).
        Connections are shared out by measured speed: a network that is much slower than the best one gets few."""
        live = [i for i in self.live() if i.name not in exclude]
        if not live:
            raise RuntimeError("no enabled network is available")
        return min(live, key=lambda i: ((self.active.get(i.name, 0) + 1) / self.weight(i.name), self.bytes.get(i.name, 0)))

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
        """Relative capacity 0.05..1 of a network. Unknown networks count as the best one, so every network gets a chance."""
        best = max((self.peak(n) for n in self._peak), default=0.0)
        mine = self.peak(name)
        if best <= 0 or mine <= 0:
            return 1.0
        return max(mine / best, MIN_WEIGHT)

    def opened(self, iface):
        self.active[iface.name] = self.active.get(iface.name, 0) + 1

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
