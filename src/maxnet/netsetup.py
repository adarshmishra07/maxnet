"""Find network ports macOS has not set up (a phone just plugged in) and ask macOS to set them up, only when the user confirms."""
import socket
import subprocess

import psutil

from .meta import labels
from .net import VIRTUAL
from .sysproxy import SUPPORTED


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


def _detect():
    """Ask macOS to create network services for any new hardware. Returns the tool's own output."""
    try:
        r = subprocess.run(["/usr/sbin/networksetup", "-detectnewhardware"], capture_output=True, text=True, timeout=30)
        return (r.stdout + r.stderr).strip()
    except (OSError, subprocess.SubprocessError) as e:
        return str(e)


def setup(name):
    """Returns (ok, message). ok only if macOS now lists `name` as a network port with a service."""
    output = _detect()
    if name in labels():
        return True, f"macOS set up {name}. It should get an address in a few seconds."
    detail = f" ({output})" if output else ""
    return False, (f"macOS did not set up {name}{detail}. Open System Settings > Network, click the ... button, "
                   "choose Add Service, pick the new port and set it to DHCP.")
