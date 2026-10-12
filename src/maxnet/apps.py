"""Attach apps to maxnet: attached apps are launched with the proxy switch / env vars set.

macOS can't force an already-running app through a proxy, so attaching takes effect the next
time the app is started via maxnet. Only Chromium-based apps accept --proxy-server."""
import socket
import subprocess
from pathlib import Path

import psutil

from .net import load_config, update_config

DIRS = [Path("/Applications"), Path.home() / "Applications"]
MARKERS = ("Electron Framework.framework", "Chromium Embedded Framework.framework")


def _is_chromium(app):
    """Chromium-based apps (Chrome, Brave, Edge, Arc, Electron apps) accept --proxy-server."""
    frameworks = app / "Contents" / "Frameworks"
    return any((frameworks / m).exists() for m in MARKERS) or (frameworks / f"{app.stem} Framework.framework").exists()


def installed():
    """Every Chromium-based or Electron app found in the Applications folders."""
    found = set()
    for d in DIRS:
        if d.is_dir():
            found |= {a.stem for a in d.glob("*.app") if _is_chromium(a)}
    return sorted(found)


def attached():
    """Everything found is attached by default; the user detaches what they do not want."""
    detached = set(load_config().get("detached", []))
    return [n for n in installed() if n not in detached]


def toggle(name):
    detached = set(load_config().get("detached", []))
    update_config(detached=sorted(detached ^ {name}))


def running(name):
    return any(f"/{name}.app/" in (p.info["exe"] or "") for p in psutil.process_iter(["exe"]))


def proxy_up(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
        return True
    except OSError:
        return False


def launch(name, port):
    """Returns a short status string. Never starts an app without a live proxy (it would have no internet)."""
    if not proxy_up(port):
        return f"{name}: proxy is not running on :{port}. Start `maxnet` first"
    if running(name):
        return f"{name}: already running. Quit it, then launch again so it picks up the proxy"
    try:
        subprocess.run(["open", "-na", name, "--args", f"--proxy-server=socks5://127.0.0.1:{port}"], check=True, capture_output=True)
    except (subprocess.CalledProcessError, OSError) as e:
        return f"{name}: could not start ({e})"
    return f"{name}: launched through maxnet"


def env_exports(port):
    """Shell exports for CLI tools. Empty when the proxy is down, so shells never lose internet."""
    if not proxy_up(port):
        return ""
    socks, http = f"socks5h://127.0.0.1:{port}", f"http://127.0.0.1:{port}"
    pairs = {"ALL_PROXY": socks, "HTTP_PROXY": http, "HTTPS_PROXY": http, "NO_PROXY": "localhost,127.0.0.1,::1"}
    lines = []
    for k, v in pairs.items():
        lines += [f"export {k}={v}", f"export {k.lower()}={v}"]
    return "\n".join(lines)
