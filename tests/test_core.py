import asyncio
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from maxnet.download import download, split_blocks
from maxnet.net import Iface, Pool
from maxnet.proxy import serve

LO = Iface("lo0" if sys.platform == "darwin" else "lo", "127.0.0.1")
DATA = os.urandom(3 * 1024 * 1024 + 123)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        r = self.headers.get("Range")
        if r:
            s, _, e = r.removeprefix("bytes=").partition("-")
            s, e = int(s), min(int(e), len(DATA) - 1)
            body = DATA[s:e + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {s}-{e}/{len(DATA)}")
        else:
            body = DATA
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def server():
    s = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield s.server_address[1]
    s.shutdown()


def pool(*ifaces):
    return Pool(enabled={i.name for i in ifaces}, lister=lambda: list(ifaces))


def test_split_blocks_covers_file_exactly():
    b = split_blocks(25, 10)
    assert b == [(0, 9), (10, 19), (20, 24)]


def test_pick_balances_by_open_connections():
    a, b = Iface("a", "1.1.1.1"), Iface("b", "2.2.2.2")
    p = pool(a, b)
    p.opened(a)
    assert p.pick() == b
    p.opened(b); p.opened(b)
    assert p.pick() == a


def test_pick_raises_when_nothing_enabled():
    with pytest.raises(RuntimeError):
        pool().pick()


def test_download_splits_and_reassembles(server, tmp_path):
    out = str(tmp_path / "f.bin")
    p = pool(LO)
    total = asyncio.run(download(f"http://127.0.0.1:{server}/f", out, p, block=512 * 1024))
    assert total == len(DATA) and open(out, "rb").read() == DATA
    assert p.bytes["lo0" if sys.platform == "darwin" else "lo"] >= len(DATA)


def test_socks5_and_http_proxy_relay(server):
    async def run():
        p = pool(LO)
        srv = await serve(p, port=0)
        port = srv.sockets[0].getsockname()[1]
        # SOCKS5 CONNECT to the local server, then a plain GET
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(b"\x05\x01\x00"); assert await r.readexactly(2) == b"\x05\x00"
        w.write(b"\x05\x01\x00\x01" + socket.inet_aton("127.0.0.1") + server.to_bytes(2, "big"))
        assert (await r.readexactly(10))[1] == 0
        w.write(b"GET /x HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        socks_body = await r.read()
        # HTTP proxy, absolute URI
        r2, w2 = await asyncio.open_connection("127.0.0.1", port)
        w2.write(f"GET http://127.0.0.1:{server}/x HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n".encode())
        http_body = await r2.read()
        srv.close()
        return socks_body, http_body, p.bytes

    s, h, counted = asyncio.run(run())
    assert DATA in s and DATA in h and sum(counted.values()) > 2 * len(DATA)


def test_shared_lines_groups_same_public_ip():
    from maxnet.meta import Meta, shared_lines
    m = {"a": Meta(public_ip="1.1.1.1"), "b": Meta(public_ip="1.1.1.1"), "c": Meta(public_ip="2.2.2.2"), "d": Meta()}
    assert shared_lines({"a", "b", "c", "d"}, m) == [["a", "b"]]
    assert shared_lines({"a", "c"}, m) == []


def test_concurrent_connections_spread_across_networks(server, monkeypatch):
    """Regression: simultaneous connects must not all pick the same network."""
    import maxnet.proxy as proxy
    real = proxy.open_via

    async def slow(iface, host, port, ssl=None, timeout=10.0):
        await asyncio.sleep(0.1)          # widen the window between pick() and connect finishing
        return await real(LO, host, port)  # fake ifaces can't be pinned, use loopback
    monkeypatch.setattr(proxy, "open_via", slow)
    ifaces = [Iface("a", "1.1.1.1"), Iface("b", "2.2.2.2"), Iface("c", "3.3.3.3")]

    async def run():
        p = pool(*ifaces)
        picked = []
        real_pick = p.pick
        p.pick = lambda **kw: (picked.append(real_pick(**kw)) or picked[-1])
        srv = await serve(p, port=0)
        port = srv.sockets[0].getsockname()[1]

        async def one():
            r, w = await asyncio.open_connection("127.0.0.1", port)
            w.write(b"\x05\x01\x00"); await r.readexactly(2)
            w.write(b"\x05\x01\x00\x01" + socket.inet_aton("127.0.0.1") + server.to_bytes(2, "big"))
            await r.readexactly(10)
            return w
        ws = await asyncio.gather(*(one() for _ in range(12)))
        srv.close()
        for w in ws:
            w.close()
        return [i.name for i in picked]

    picked = asyncio.run(run())
    assert len(picked) == 12 and all(picked.count(n) == 4 for n in "abc")


def test_sysproxy_keeps_original_backup_and_restores(monkeypatch, tmp_path):
    import maxnet.net as net, maxnet.sysproxy as sp
    monkeypatch.setattr(sp, "SUPPORTED", True)
    calls = []
    orig = {"enabled": True, "server": "10.9.9.9", "port": "8080", "bypass": ["x.test"]}
    monkeypatch.setattr(sp, "services", lambda: ["Wi-Fi"])
    snaps = iter([orig, {"enabled": True, "server": "127.0.0.1", "port": "1080", "bypass": sp.BYPASS}])
    monkeypatch.setattr(sp, "_snapshot", lambda s: next(snaps))
    monkeypatch.setattr(sp, "_run", lambda a: calls.append(a) or "")
    assert sp.enable(1080) and sp.active()
    sp.enable(1080)                      # second enable (e.g. after a crash) must keep the ORIGINAL backup
    assert sp.disable() and not sp.active()
    assert ["-setsocksfirewallproxy", "Wi-Fi", "10.9.9.9", "8080"] in calls
    assert ["-setproxybypassdomains", "Wi-Fi", "x.test"] in calls
    assert sp.disable() is False         # nothing left to restore


def test_sysproxy_refuses_invalid_port(monkeypatch):
    import maxnet.sysproxy as sp
    monkeypatch.setattr(sp, "SUPPORTED", True)
    called = []
    monkeypatch.setattr(sp, "_run", lambda a: called.append(a) or "")
    assert sp.enable(0) is False and called == []


@pytest.mark.parametrize("url,expected", [
    ("https://x.test/a/b/file.iso", "file.iso"),
    ("https://x.test/..%2f..%2fetc%2fpasswd", "passwd"),
    ("https://x.test/%2e%2e", "download"),
    ("https://x.test/a%5c..%5cevil.bin", "evil.bin"),
    ("https://x.test/", "download"),
])
def test_safe_filename_never_escapes(url, expected):
    from maxnet.download import safe_filename
    assert safe_filename(url) == expected


def test_download_spreads_blocks_across_all_networks(server, tmp_path, monkeypatch):
    """Regression: a small file used to be taken entirely by the first network's workers."""
    import maxnet.download as dl
    real = dl.open_via

    async def via_loopback(iface, host, port, ssl=None, timeout=10.0):
        return await real(LO, host, port, ssl=ssl)  # fake ifaces can't be pinned, route all to loopback
    monkeypatch.setattr(dl, "open_via", via_loopback)
    ifaces = [Iface("a", "1.1.1.1"), Iface("b", "2.2.2.2"), Iface("c", "3.3.3.3")]
    p = pool(*ifaces)
    out = str(tmp_path / "f.bin")
    asyncio.run(dl.download(f"http://127.0.0.1:{server}/f", out, p))
    assert open(out, "rb").read() == DATA
    assert all(p.bytes.get(n, 0) > 0 for n in "abc"), p.bytes


def test_open_via_fails_fast_when_network_cannot_reach_host():
    """Regression: an unreachable network used to hang forever and stall the whole download."""
    import time
    from maxnet.net import open_via

    async def run():
        t0 = time.monotonic()
        with pytest.raises(OSError):  # ConnectionError (timeout) or an immediate "network unreachable", both fail fast
            await open_via(LO, "10.255.255.1", 9, timeout=0.5)
        return time.monotonic() - t0
    assert asyncio.run(run()) < 3


def test_download_survives_a_network_that_cannot_reach_the_server(server, tmp_path, monkeypatch):
    """Regression: one dead network (first in the list) must not fail the download or stall it."""
    import maxnet.download as dl
    real = dl.open_via

    async def flaky(iface, host, port, ssl=None, timeout=10.0):
        if iface.name == "dead":
            raise ConnectionError("dead could not reach the host")
        return await real(LO, host, port, ssl=ssl)
    monkeypatch.setattr(dl, "open_via", flaky)
    ifaces = [Iface("dead", "9.9.9.9"), Iface("b", "2.2.2.2"), Iface("c", "3.3.3.3")]
    p = pool(*ifaces)
    out = str(tmp_path / "f.bin")
    asyncio.run(dl.download(f"http://127.0.0.1:{server}/f", out, p))
    assert open(out, "rb").read() == DATA
    assert p.bytes.get("dead", 0) == 0 and p.bytes["b"] > 0 and p.bytes["c"] > 0


def test_download_survives_a_network_that_connects_but_never_sends(server, tmp_path, monkeypatch):
    """Regression: a stalled stream used to hang the download forever."""
    import maxnet.download as dl
    real = dl.open_via
    monkeypatch.setattr(dl, "STALL", 0.5)

    class Silent:
        async def read(self, n):
            await asyncio.sleep(3600)
        async def readuntil(self, sep):
            await asyncio.sleep(3600)

    class W:
        def write(self, d): pass
        def close(self): pass

    async def maybe_silent(iface, host, port, ssl=None, timeout=10.0):
        if iface.name == "mute":
            return Silent(), W()
        return await real(LO, host, port, ssl=ssl)
    monkeypatch.setattr(dl, "open_via", maybe_silent)
    p = pool(Iface("mute", "9.9.9.9"), Iface("b", "2.2.2.2"))
    out = str(tmp_path / "f.bin")
    asyncio.run(asyncio.wait_for(dl.download(f"http://127.0.0.1:{server}/f", out, p), 30))
    assert open(out, "rb").read() == DATA


def test_pick_skips_excluded_networks():
    a, b = Iface("a", "1.1.1.1"), Iface("b", "2.2.2.2")
    p = pool(a, b)
    assert p.pick(exclude={"a"}) == b
    with pytest.raises(RuntimeError):
        p.pick(exclude={"a", "b"})


def test_proxy_skips_a_network_that_just_dropped(server, monkeypatch):
    """A network that fails to connect must not fail the request: the next network is tried."""
    import maxnet.proxy as proxy
    real = proxy.open_via

    async def flaky(iface, host, port, ssl=None, timeout=10.0):
        if iface.name == "gone":
            raise ConnectionError("network unreachable")
        return await real(LO, host, port)
    monkeypatch.setattr(proxy, "open_via", flaky)

    async def run():
        p = pool(Iface("gone", "9.9.9.9"), Iface("up", "2.2.2.2"))
        srv = await serve(p, port=0)
        port = srv.sockets[0].getsockname()[1]
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(f"GET http://127.0.0.1:{server}/x HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n".encode())
        body = await r.read()
        srv.close()
        return body, p.active
    body, active = asyncio.run(run())
    assert DATA in body and active == {"gone": 0, "up": 0}  # served, and no connection slot leaked


def test_tui_pauses_system_proxy_when_all_networks_drop_and_resumes(monkeypatch):
    """Simulated disconnect (no real cable to pull in CI): proxy goes OFF with none left, back ON when one returns."""
    import maxnet.tui as tui
    state = {"on": False, "ifaces": [Iface("a", "1.1.1.1")], "enabled": 0, "disabled": 0}

    def enable(port):
        state.update(on=True, enabled=state["enabled"] + 1)
        return True

    def disable():
        state.update(on=False, disabled=state["disabled"] + 1)
        return True
    monkeypatch.setattr(tui.sysproxy, "enable", enable)
    monkeypatch.setattr(tui.sysproxy, "disable", disable)
    monkeypatch.setattr(tui.sysproxy, "active", lambda: state["on"])
    monkeypatch.setattr(tui, "list_interfaces", lambda: state["ifaces"])
    monkeypatch.setattr(tui, "PORT", 0)

    async def run():
        app = tui.MaxNet()
        app.pool = Pool(enabled={"a"}, lister=lambda: state["ifaces"])
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause(0.3)
            assert state["on"] is True and app.proxy_ok  # it turned itself ON at startup
            state["ifaces"] = []                 # every network disconnects
            app.tick(); await pilot.pause(0.1)
            assert state["on"] is False and app.auto_paused is True
            state["ifaces"] = [Iface("a", "1.1.1.1")]  # one comes back
            app.tick(); await pilot.pause(0.1)
            assert state["on"] is True and app.auto_paused is False
    asyncio.run(run())


def test_netsetup_finds_only_link_local_ports_without_a_service(monkeypatch):
    import maxnet.netsetup as ns
    from collections import namedtuple
    A = namedtuple("A", "family address")
    S = namedtuple("S", "isup")
    monkeypatch.setattr(ns, "SUPPORTED", True)
    monkeypatch.setattr(ns, "labels", lambda: {"en0": "Wi-Fi"})
    monkeypatch.setattr(ns.psutil, "net_if_addrs", lambda: {
        "en0": [A(socket.AF_INET, "169.254.1.1")],     # has a service already: skip
        "en11": [A(socket.AF_INET, "169.254.12.91")],  # new phone, no service: report
        "en12": [A(socket.AF_INET, "192.168.1.5")],    # real address: skip
        "utun3": [A(socket.AF_INET, "169.254.9.9")],   # virtual: skip
        "en13": [A(socket.AF_INET, "169.254.7.7")],    # down: skip
    })
    monkeypatch.setattr(ns.psutil, "net_if_stats", lambda: {n: S(n != "en13") for n in ["en0", "en11", "en12", "utun3", "en13"]})
    assert ns.unconfigured() == ["en11"]


def test_netsetup_reports_success_only_when_macos_really_lists_the_port(monkeypatch):
    import maxnet.netsetup as ns
    monkeypatch.setattr(ns, "_detect", lambda: "")
    monkeypatch.setattr(ns, "labels", lambda: {})                       # macOS did NOT create it
    ok, msg = ns.setup("en11")
    assert ok is False and "did not set up en11" in msg and "Add Service" in msg
    monkeypatch.setattr(ns, "labels", lambda: {"en11": "Ethernet Adapter (en11)"})  # now it did
    ok, msg = ns.setup("en11")
    assert ok is True


def test_health_check_reports_real_proxy_path(server):
    from maxnet.check import run_checks

    async def run():
        p = pool(LO)
        srv = await serve(p, port=0)
        port = srv.sockets[0].getsockname()[1]
        # the checker fetches example.com through the proxy; here "example.com" is the local test server
        import maxnet.check as chk
        async def fake_via(port_, host="x"):
            r, w = await asyncio.open_connection("127.0.0.1", port_)
            w.write(f"GET http://127.0.0.1:{server}/ HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n".encode())
            body = await r.read(); w.close()
            return len(body) > 0
        chk._via_proxy = fake_via
        rows = await run_checks(p, port, False, None)
        srv.close()
        return rows
    rows = asyncio.run(run())
    assert any("through the maxnet proxy worked" in t and ok for ok, t in rows)


def test_apps_autodetects_chromium_and_electron_and_attaches_all_by_default(monkeypatch, tmp_path):
    import maxnet.apps as apps
    def make(name, framework):
        d = tmp_path / f"{name}.app" / "Contents" / "Frameworks" / framework
        d.mkdir(parents=True)
    make("Slack", "Electron Framework.framework")                 # Electron
    make("Spotify", "Chromium Embedded Framework.framework")      # CEF
    make("Google Chrome", "Google Chrome Framework.framework")    # Chrome style
    make("Safari", "WebKit.framework")                            # not Chromium: must be ignored
    (tmp_path / "Notes.app" / "Contents").mkdir(parents=True)     # no frameworks at all
    monkeypatch.setattr(apps, "DIRS", [tmp_path])
    assert apps.installed() == ["Google Chrome", "Slack", "Spotify"]
    assert apps.attached() == ["Google Chrome", "Slack", "Spotify"]      # all attached by default
    apps.toggle("Slack")
    assert apps.attached() == ["Google Chrome", "Spotify"]               # detach one
    apps.toggle("Slack")
    assert "Slack" in apps.attached()                                    # and re-attach
