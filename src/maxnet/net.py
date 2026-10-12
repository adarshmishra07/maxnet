"""Interfaces, the pool that picks one per connection, and sockets pinned to an interface."""
import asyncio
import json
import os
import socket
import sys
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


class Pool:
    """Tracks enabled interfaces and per-interface live counters."""

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

    def live(self):
        return [i for i in self.lister() if i.name in self.enabled]

    def toggle(self, name):
        self.enabled = self.enabled ^ {name}
        save_enabled(self.enabled)

    def pick(self, exclude=()):
        """Least-busy enabled network, skipping the names in `exclude` (ones that just failed)."""
        live = [i for i in self.live() if i.name not in exclude]
        if not live:
            raise RuntimeError("no enabled network is available")
        # least open connections wins, so a slow link (long-lived conns) gets fewer new ones
        return min(live, key=lambda i: (self.active.get(i.name, 0), self.bytes.get(i.name, 0)))

    def opened(self, iface):
        self.active[iface.name] = self.active.get(iface.name, 0) + 1

    def closed(self, iface):
        self.active[iface.name] = max(0, self.active.get(iface.name, 0) - 1)

    def count(self, iface, n):
        self.bytes[iface.name] = self.bytes.get(iface.name, 0) + n


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
