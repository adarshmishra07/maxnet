"""macOS system SOCKS proxy on/off, with the user's original settings saved and restored.

The backup lives in the config file, so a crash (even SIGKILL) can be undone later with
`maxnet system off`; enabling never overwrites an existing backup with our own settings."""
import subprocess
import sys

from .net import load_config, update_config

BYPASS = ["*.local", "169.254/16", "localhost", "127.0.0.1", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]
SUPPORTED = sys.platform == "darwin"


def _run(args):
    try:
        return subprocess.run(["networksetup", *args], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def services():
    return [s for s in _run(["-listallnetworkservices"]).splitlines()[1:] if s and not s.startswith("*")]


def _snapshot(svc):
    fields = dict(l.split(": ", 1) for l in _run(["-getsocksfirewallproxy", svc]).splitlines() if ": " in l)
    bypass = _run(["-getproxybypassdomains", svc]).splitlines()
    return {"enabled": fields.get("Enabled") == "Yes", "server": fields.get("Server", ""),
            "port": fields.get("Port", "0"), "bypass": [] if not bypass or bypass[0].startswith("There aren") else bypass}


def active():
    return bool(load_config().get("sysproxy_backup"))


def enable(port):
    """Point every network service's SOCKS proxy at maxnet. False if unsupported."""
    if not SUPPORTED or not 0 < port < 65536:  # never point system settings at a dead/invalid port
        return False
    svcs = services()
    if not active():  # keep the ORIGINAL settings; a stale backup must survive a re-enable
        update_config(sysproxy_backup={s: _snapshot(s) for s in svcs})
    for s in svcs:
        _run(["-setsocksfirewallproxy", s, "127.0.0.1", str(port)])
        _run(["-setsocksfirewallproxystate", s, "on"])
        _run(["-setproxybypassdomains", s, *BYPASS])
    return True


def disable():
    """Restore the saved settings. False if nothing was enabled by us."""
    backup = load_config().get("sysproxy_backup")
    if not (SUPPORTED and backup):
        return False
    for s, old in backup.items():
        if old["enabled"] and old["server"]:
            _run(["-setsocksfirewallproxy", s, old["server"], old["port"]])
        else:
            _run(["-setsocksfirewallproxystate", s, "off"])
        _run(["-setproxybypassdomains", s, *(old["bypass"] or ["Empty"])])
    update_config(sysproxy_backup=None)
    return True
