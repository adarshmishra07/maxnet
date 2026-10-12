"""Find network ports macOS has not set up (a phone just plugged in) and set them up, only when the user confirms."""
import socket

import psutil

from .meta import labels
from .net import VIRTUAL
from .sysproxy import SUPPORTED, _run


def unconfigured():
    """Up ports that only have a link-local (169.254.x.x) address and no macOS network service."""
    if not SUPPORTED:
        return []
    known = labels()
    stats = psutil.net_if_stats()
    out = []
    for name, addrs in psutil.net_if_addrs().items():
        v4 = [a.address for a in addrs if a.family == socket.AF_INET]
        if name.startswith(VIRTUAL) or name in known or not stats[name].isup:
            continue
        if v4 and all(a.startswith("169.254.") for a in v4):
            out.append(name)
    return sorted(out)


def setup(name):
    """Create a network service for `name` and set it to DHCP. Changes macOS Network settings."""
    service = f"maxnet {name}"
    _run(["-createnetworkservice", service, name])
    _run(["-setdhcp", service])
    return service
