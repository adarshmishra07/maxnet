"""Per-network details: friendly name, link speed, public IP / ISP / city, latency, default route."""
import asyncio
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass

import psutil

from .download import request
from .net import open_via

PING_HOST = ("1.1.1.1", 443)
IP_API = "http://ip-api.com/json/?fields=status,query,isp,city,countryCode"


@dataclass(frozen=True)
class Meta:
    label: str = ""
    link_mbit: int = 0
    public_ip: str = ""
    isp: str = ""
    city: str = ""
    latency_ms: float | None = None
    jitter_ms: float = 0.0
    reachable: bool | None = None  # None = not probed yet
    is_default: bool = False


def _run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def mask_ip(ip):
    """203.0.113.7 -> 203.•••.•••.•••  (keeps only the first octet)."""
    return ip.split(".")[0] + ".•••.•••.•••" if ip else ""


def labels():
    """name -> human label (Wi-Fi, USB LAN, ...)."""
    out = {}
    if sys.platform == "darwin":
        port = None
        for line in _run(["networksetup", "-listallhardwareports"]).splitlines():
            if line.startswith("Hardware Port:"):
                port = line.split(":", 1)[1].strip()
            elif line.startswith("Device:") and port:
                out[line.split(":", 1)[1].strip()] = port
    else:
        for name in psutil.net_if_addrs():
            out[name] = "Wi-Fi" if os.path.isdir(f"/sys/class/net/{name}/wireless") else "Ethernet"
    return out


def default_interface():
    if sys.platform == "darwin":
        for line in _run(["route", "-n", "get", "default"]).splitlines():
            if "interface:" in line:
                return line.split(":", 1)[1].strip()
    else:
        parts = _run(["ip", "route", "show", "default"]).split()
        if "dev" in parts:
            return parts[parts.index("dev") + 1]
    return ""


async def _latency(iface, n=3):
    times = []
    for _ in range(n):
        t0 = time.monotonic()
        try:
            _, w = await asyncio.wait_for(open_via(iface, *PING_HOST), 3)
        except (OSError, asyncio.TimeoutError):
            continue
        times.append((time.monotonic() - t0) * 1000)
        w.close()
    return (sum(times) / len(times), max(times) - min(times)) if times else (None, 0.0)


async def _whois(iface):
    if os.environ.get("MAXNET_NO_LOOKUP"):  # privacy opt-out: skip the public IP / ISP lookup
        return {}
    try:
        _, _, r, w = await asyncio.wait_for(request(iface, IP_API, {}), 6)
        body = (await asyncio.wait_for(r.read(), 6)).decode("utf-8", "replace")
        w.close()
        data = json.loads(body[body.index("{"):body.rindex("}") + 1])
        return data if data.get("status") == "success" else {}
    except (OSError, ValueError, asyncio.TimeoutError):
        return {}


async def probe(iface, label="", default=""):
    (ms, jitter), who = await asyncio.gather(_latency(iface), _whois(iface))
    stats = psutil.net_if_stats().get(iface.name)
    return Meta(
        label=label or iface.name,
        link_mbit=stats.speed if stats else 0,
        public_ip=who.get("query", ""),
        isp=who.get("isp", ""),
        city=f"{who.get('city', '')}, {who['countryCode']}" if who.get("city") else "",
        latency_ms=ms,
        jitter_ms=jitter,
        reachable=ms is not None,
        is_default=iface.name == default,
    )


async def probe_all(ifaces):
    lab, default = labels(), default_interface()
    metas = await asyncio.gather(*(probe(i, lab.get(i.name, ""), default) for i in ifaces))
    return {i.name: m for i, m in zip(ifaces, metas)}


def shared_lines(enabled, metas):
    """Groups of enabled networks that share a public IP (one physical line)."""
    groups = {}
    for n in enabled:
        ip = metas.get(n, Meta()).public_ip
        if ip:
            groups.setdefault(ip, []).append(n)
    return [sorted(g) for g in groups.values() if len(g) > 1]
