"""Attach apps to maxnet: attached apps are launched with the proxy switch / env vars set.

macOS can't force an already-running app through a proxy, so attaching takes effect the next
time the app is started via maxnet. Only Chromium-based apps accept --proxy-server."""
import socket
import subprocess
from pathlib import Path

import psutil

from .net import load_config, update_config

CHROMIUM = ["Google Chrome", "Brave Browser", "Microsoft Edge", "Arc", "Vivaldi",
            "Slack", "Discord", "Visual Studio Code", "Claude", "Notion"]
DEFAULT_ON = CHROMIUM  # everything supported is attached by default; detach what you do not want
DIRS = [Path("/Applications"), Path.home() / "Applications"]


def installed():
    return [n for n in CHROMIUM if any((d / f"{n}.app").exists() for d in DIRS)]


def attached():
    chosen = load_config().get("attached", DEFAULT_ON)
    return [n for n in installed() if n in chosen]


def toggle(name):
    update_config(attached=sorted(set(attached()) ^ {name}))


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
    subprocess.run(["open", "-na", name, "--args", f"--proxy-server=socks5://127.0.0.1:{port}"], check=True)
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
