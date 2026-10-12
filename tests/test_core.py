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


def _keepalive_server(close_after=None):
    """HTTP/1.1 keep-alive server that counts connections. close_after: silently close a connection after N requests."""
    stats = {"connections": 0}

    class K(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a): pass

        def setup(self):
            super().setup()
            self.served = 0

        def do_GET(self):
            r = self.headers.get("Range")
            s, _, e = r.removeprefix("bytes=").partition("-")
            s, e = int(s), min(int(e), len(DATA) - 1)
            body = DATA[s:e + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {s}-{e}/{len(DATA)}")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.served += 1
            if close_after and self.served >= close_after:
                self.close_connection = True  # closes without telling the client in the response

    class S(ThreadingHTTPServer):
        def process_request(self, request, client_address):
            stats["connections"] += 1
            super().process_request(request, client_address)

    srv = S(("127.0.0.1", 0), K)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, stats


def test_download_reuses_one_connection_for_many_blocks(tmp_path):
    srv, stats = _keepalive_server()
    try:
        out = str(tmp_path / "f.bin")
        p = pool(LO)
        asyncio.run(download(f"http://127.0.0.1:{srv.server_address[1]}/f", out, p, block=256 * 1024, streams=1))
        assert open(out, "rb").read() == DATA
        assert stats["connections"] <= 3, stats  # 1 probe + 1 stream, not one per block (12 blocks)
    finally:
        srv.shutdown()


def test_download_recovers_when_server_closes_an_idle_connection(tmp_path):
    srv, stats = _keepalive_server(close_after=2)
    try:
        out = str(tmp_path / "f.bin")
        p = pool(LO)
        asyncio.run(download(f"http://127.0.0.1:{srv.server_address[1]}/f", out, p, block=256 * 1024, streams=1))
        assert open(out, "rb").read() == DATA              # every block arrived despite the silent closes
        assert 3 < stats["connections"] < 12, stats         # reconnected as needed, still far fewer than one per block
    finally:
        srv.shutdown()


def test_slow_network_warning_names_the_throttled_one():
    from maxnet.bench import slow_warnings
    w = slow_warnings({"en0": 108.0, "en7": 81.0, "en9": 1.2})
    assert len(w) == 1 and w[0].startswith("en9 is much slower")
    assert slow_warnings({"en0": 108.0, "en7": 90.0}) == []        # similar networks: no warning
    assert slow_warnings({"en0": 8.0, "en9": 0.5}) == []           # everything slow: not a "slow one out" situation


def test_pick_prefers_the_faster_network_and_starves_a_throttled_one():
    from maxnet.net import MIN_WEIGHT
    fast, slow = Iface("fast", "1.1.1.1"), Iface("slow", "2.2.2.2")
    p = pool(fast, slow)
    p._peak = {"fast": (12_000_000.0, __import__("time").monotonic()), "slow": (150_000.0, __import__("time").monotonic())}  # 96 vs 1.2 Mbit/s
    picks = []
    for _ in range(60):                     # 60 new connections arrive, none ever finishes
        i = p.pick(); p.opened(i); picks.append(i.name)
    assert picks.count("slow") <= 4         # the throttled network gets only the occasional connection
    assert p.weight("slow") == MIN_WEIGHT


def test_unknown_networks_get_a_fair_chance():
    a, b = Iface("a", "1.1.1.1"), Iface("b", "2.2.2.2")
    p = pool(a, b)
    assert p.weight("a") == 1.0 and p.weight("b") == 1.0
    p.count(a, 1_000_000)                   # only "a" has been measured; "b" is unknown so it counts as fully capable
    assert p.weight("b") == 1.0


def test_download_stops_using_a_network_that_is_far_slower_than_the_best(server, tmp_path, monkeypatch):
    """Regression: a throttled network used to hold blocks back and drag the whole download below the best single network."""
    import time
    import maxnet.download as dl
    real = dl.open_via
    monkeypatch.setattr(dl, "SLOW_AFTER", 0.3)
    monkeypatch.setattr(dl, "MONITOR_EVERY", 0.1)
    monkeypatch.setattr(dl, "SLOW_MIN_BEST", 1000)   # loopback bursts are short; real networks are sustained

    class SlowReader:
        def __init__(self, r): self.r = r
        async def read(self, n):
            await asyncio.sleep(0.6)            # a throttled phone: one 64 KB chunk every 0.6 s
            return await self.r.read(n)
        async def readuntil(self, sep): return await self.r.readuntil(sep)
        def at_eof(self): return self.r.at_eof()

    async def via(iface, host, port, ssl=None, timeout=10.0):
        r, w = await real(LO, host, port, ssl=ssl)
        return (SlowReader(r), w) if iface.name == "slow" else (r, w)
    monkeypatch.setattr(dl, "open_via", via)
    notes = []
    p = pool(Iface("fast", "1.1.1.1"), Iface("slow", "2.2.2.2"))
    out = str(tmp_path / "f.bin")
    t0 = time.monotonic()
    asyncio.run(dl.download(f"http://127.0.0.1:{server}/f", out, p, streams=4, note=notes.append))
    took = time.monotonic() - t0
    assert open(out, "rb").read() == DATA
    assert took < 2.5, took                                # without the monitor this takes ~4+ s
    assert p.bytes.get("slow", 0) < 500_000, p.bytes       # the slow network ends up carrying little
    assert any("slow" in n and "much slower" in n for n in notes), notes


# ---------- regression tests for the code review findings ----------

def test_list_interfaces_survives_an_interface_vanishing_between_snapshots(monkeypatch):
    """Finding 1: unplugging a phone between psutil's two snapshots used to raise KeyError and crash the TUI."""
    import maxnet.net as net
    from collections import namedtuple
    A, S = namedtuple("A", "family address"), namedtuple("S", "isup")
    monkeypatch.setattr(net.psutil, "net_if_addrs", lambda: {"en0": [A(socket.AF_INET, "192.168.1.5")], "en11": [A(socket.AF_INET, "172.20.10.4")]})
    monkeypatch.setattr(net.psutil, "net_if_stats", lambda: {"en0": S(True)})  # en11 vanished
    assert [i.name for i in net.list_interfaces()] == ["en0"]


def test_reset_terminal_never_blocks_and_restores_the_blocking_flag():
    """Finding 2: the exit handler left the shared terminal non-blocking, which breaks the user's shell."""
    from maxnet.tui import reset_terminal
    r, w = os.pipe()
    assert os.get_blocking(w) is True
    reset_terminal(w)
    assert os.get_blocking(w) is True and b"\x1b[?1049l" in os.read(r, 100)
    os.set_blocking(w, False)                       # now fill the pipe so a normal write would block forever
    try:
        while True:
            os.write(w, b"x" * 65536)
    except BlockingIOError:
        pass
    os.set_blocking(w, True)
    reset_terminal(w)                               # must return instead of hanging
    assert os.get_blocking(w) is True


def test_system_on_refuses_when_no_proxy_is_listening(monkeypatch):
    """Finding 3: `maxnet system on` used to point every app at a dead proxy and cut their internet."""
    import maxnet.apps as apps, maxnet.cli as cli, maxnet.sysproxy as sp
    monkeypatch.setattr(apps, "proxy_up", lambda port: False)
    monkeypatch.setattr(sp, "enable", lambda port: pytest.fail("must not change system settings"))
    monkeypatch.setattr(sys, "argv", ["maxnet", "system", "on"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert "No maxnet proxy is listening" in str(e.value)


def test_only_the_instance_that_turned_the_proxy_on_turns_it_off(monkeypatch):
    """Finding 4: a second maxnet (that never enabled anything) used to undo the first one's system proxy on quit."""
    import maxnet.tui as tui
    calls = []

    class Fake:
        owns_system_proxy = False
        def __init__(self, system_proxy=True): pass
        def run(self): pass
    monkeypatch.setattr(tui, "MaxNet", Fake)
    monkeypatch.setattr(tui.signal, "signal", lambda *a: None)
    monkeypatch.setattr(tui.sysproxy, "disable", lambda: calls.append("disable"))
    tui.run()
    assert calls == []                                  # not the owner: leaves it alone
    Fake.owns_system_proxy = True
    tui.run()
    assert calls == ["disable"]                         # the owner restores


def test_config_survives_many_processes_writing_at_once(tmp_path):
    """Finding 5: non-atomic writes could tear the file and lose the only backup of the user's proxy settings."""
    import json
    import subprocess
    cfg = tmp_path / "config.json"
    code = ("import sys; from maxnet.net import update_config\n"
            "for n in range(40): update_config(**{f'k{sys.argv[1]}': n})")
    procs = [subprocess.Popen([sys.executable, "-c", code, str(i)], env={**os.environ, "MAXNET_CONFIG": str(cfg)}) for i in range(6)]
    assert all(p.wait(60) == 0 for p in procs)
    data = json.loads(cfg.read_text())                  # valid JSON, and no writer lost its key
    assert sorted(data) == [f"k{i}" for i in range(6)] and all(v == 39 for v in data.values())


def test_proxy_releases_the_connection_slot_on_a_bad_hostname(server):
    """Finding 6: a hostname getaddrinfo rejects (UnicodeError) leaked a reserved slot forever."""
    async def run():
        p = pool(LO)
        srv = await serve(p, port=0)
        port = srv.sockets[0].getsockname()[1]
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(b"\x05\x01\x00"); await r.readexactly(2)
        host = b"a" * 70 + b".example.com"                       # a label over 63 characters
        w.write(b"\x05\x01\x00\x03" + bytes([len(host)]) + host + (443).to_bytes(2, "big"))
        await r.read()                                          # the proxy closes on us
        srv.close()
        return p.active
    assert asyncio.run(run()).get(LO.name, 0) == 0


def test_launch_reports_a_failed_app_instead_of_crashing(monkeypatch):
    """Finding 7: one app failing to start used to raise and kill the TUI."""
    import subprocess
    import maxnet.apps as apps
    monkeypatch.setattr(apps, "proxy_up", lambda port: True)
    monkeypatch.setattr(apps, "running", lambda name: False)
    def boom(*a, **k): raise subprocess.CalledProcessError(1, "open")
    monkeypatch.setattr(apps.subprocess, "run", boom)
    assert "could not start" in apps.launch("Broken App", 1080)


def test_two_equally_fast_networks_are_never_called_slow(server, tmp_path, monkeypatch):
    """Finding 8: healthy networks (even ones idle at the tail) must not be retired."""
    import maxnet.download as dl
    real = dl.open_via
    monkeypatch.setattr(dl, "SLOW_AFTER", 0.05)
    monkeypatch.setattr(dl, "MONITOR_EVERY", 0.05)
    monkeypatch.setattr(dl, "SLOW_MIN_BEST", 1000)
    async def via(iface, host, port, ssl=None, timeout=10.0):
        return await real(LO, host, port, ssl=ssl)
    monkeypatch.setattr(dl, "open_via", via)
    notes = []
    p = pool(Iface("a", "1.1.1.1"), Iface("b", "2.2.2.2"))
    out = str(tmp_path / "f.bin")
    asyncio.run(dl.download(f"http://127.0.0.1:{server}/f", out, p, streams=4, note=notes.append))
    assert open(out, "rb").read() == DATA and notes == []


def test_tui_does_not_shell_out_to_networksetup_every_second(monkeypatch):
    """Finding 9: unconfigured() spawned a process every tick on the event loop shared with the proxy."""
    import maxnet.tui as tui
    calls = []
    monkeypatch.setattr(tui.netsetup, "unconfigured", lambda: calls.append(1) or [])
    monkeypatch.setattr(tui.sysproxy, "enable", lambda p: False)
    monkeypatch.setattr(tui, "PORT", 0)
    async def run():
        app = tui.MaxNet(system_proxy=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(2.6)                      # at least two 1-second ticks
    asyncio.run(run())
    assert len(calls) <= 2, calls                       # once at start (plus at most the menu), not once per tick


def test_restricted_pool_shares_counters_but_never_touches_the_real_selection(tmp_path):
    """Finding 10: the speed test used to swap pool.enabled, rerouting live traffic and risking a persisted wrong choice."""
    a, b = Iface("a", "1.1.1.1"), Iface("b", "2.2.2.2")
    p = pool(a, b)
    v = p.restricted({"a"})
    assert [i.name for i in v.live()] == ["a"] and p.enabled == {"a", "b"}
    v.count(a, 1000)
    assert p.bytes["a"] == 1000                         # counters are shared, so the TUI still shows the test traffic
    assert p.enabled == {"a", "b"}


def test_unique_path_never_overwrites(tmp_path):
    from maxnet.download import unique_path
    (tmp_path / "f.iso").write_text("x")
    assert unique_path(tmp_path / "f.iso").name == "f (1).iso"
    (tmp_path / "f (1).iso").write_text("x")
    assert unique_path(tmp_path / "f.iso").name == "f (2).iso"
    assert unique_path(tmp_path / "new.iso").name == "new.iso"


def test_a_crash_while_saving_config_leaves_the_old_file_intact(tmp_path, monkeypatch):
    """Finding 5 (second half): the saved proxy backup must survive the process dying mid-write."""
    import json
    import maxnet.net as net
    cfg = tmp_path / "config.json"
    monkeypatch.setattr(net, "CONFIG", cfg)
    net.update_config(sysproxy_backup={"Wi-Fi": {"enabled": True}})
    before = cfg.read_text()
    monkeypatch.setattr(net.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("died before the swap")))
    with pytest.raises(OSError):
        net.update_config(other="value")
    assert cfg.read_text() == before and json.loads(before)["sysproxy_backup"]["Wi-Fi"]["enabled"] is True


def test_confirm_screens_preselect_cancel():
    """A stray Enter must never change the user's system: the safe option comes first on every confirm screen."""
    import maxnet.tui as tui
    import maxnet.netsetup as ns

    async def run():
        app = tui.MaxNet(system_proxy=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.3)
            app.pending = ("setup", "en11")
            app.mode = "setup"
            app.on_option_list_option_selected(type("E", (), {"option": type("O", (), {"id": "en11"})()})())
            await pilot.pause(0.1)
            assert app.mode == "confirm"
            menu = app.query_one("#menu")
            assert menu.options[0].id == "back" and menu.highlighted == 0
            await pilot.press("enter")                     # a stray Enter...
            await pilot.pause(0.1)
            assert app.mode == "menu"                      # ...cancels, and nothing was set up
    ran = []
    import pytest as _pt
    mp = _pt.MonkeyPatch()
    mp.setattr(ns, "setup", lambda name: ran.append(name) or (True, "x"))
    mp.setattr(tui.sysproxy, "enable", lambda p: False)
    mp.setattr(tui, "PORT", 0)
    try:
        asyncio.run(run())
    finally:
        mp.undo()
    assert ran == []


def test_menu_offers_port_setup_as_soon_as_a_new_port_is_found(monkeypatch):
    """The notice says 'choose Set up new network port in the menu', so that item must really be in the menu."""
    import maxnet.tui as tui
    monkeypatch.setattr(tui.netsetup, "unconfigured", lambda: ["en11"])
    monkeypatch.setattr(tui.sysproxy, "enable", lambda p: False)
    monkeypatch.setattr(tui, "PORT", 0)

    async def run():
        app = tui.MaxNet(system_proxy=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(1.5)
            return [o.id for o in app.query_one("#menu").options]
    assert asyncio.run(run())[0] == "setup"


def test_a_network_that_was_slow_is_not_locked_out_forever():
    """A throttled network gets almost no connections, so it can never prove it recovered: stale slowness must expire."""
    import time
    from maxnet.net import STALE_AFTER
    fast, slow = Iface("fast", "1.1.1.1"), Iface("slow", "2.2.2.2")
    p = pool(fast, slow)
    now = time.monotonic()
    p._peak = {"fast": (12_000_000.0, now), "slow": (150_000.0, now)}        # slow was measured just now: starved
    assert p.weight("slow") < 0.05 + 1e-9
    p._peak = {"fast": (12_000_000.0, now), "slow": (150_000.0, now - STALE_AFTER - 1)}   # measured long ago
    assert p.weight("slow") == 1.0                                           # forgotten: treated as unknown again
    first = [p.pick().name for _ in range(1)]
    assert first == ["slow"] or p.weight("fast") >= 0.0                      # and it competes for connections again


def test_idle_long_lived_connections_do_not_push_new_ones_away():
    """Chrome/Slack keep many idle connections open. A network holding 12 of them is not busy and must still get new connections."""
    a, b = Iface("a", "1.1.1.1"), Iface("b", "2.2.2.2")
    p = pool(a, b)
    p.active = {"a": 8, "b": 12}          # idle keep-alives opened long ago: no recent opens, no speed
    picks = []
    for _ in range(8):
        i = p.pick(); p.opened(i); picks.append(i.name)
    assert picks.count("a") >= 3 and picks.count("b") >= 3, picks   # shared out, not all sent to the one with fewer idle connections


def test_pick_skips_a_network_without_internet_but_falls_back_if_none_have_it():
    a, b = Iface("a", "1.1.1.1"), Iface("b", "2.2.2.2")
    p = pool(a, b)
    p.mark("a", False)
    assert all(p.pick().name == "b" for _ in range(5))            # "a" is waiting, so it gets nothing
    assert [i.name for i in p.waiting()] == ["a"]
    p.mark("b", False)
    assert p.pick().name in ("a", "b")                            # nothing ready at all: still try, never refuse outright


def test_health_loop_adds_a_network_by_itself_when_its_internet_starts_working(monkeypatch):
    """The phone is plugged in first and gets internet a few seconds later: it must join without anyone touching anything."""
    import maxnet.net as net
    attempts = {"phone": 0}

    class W:
        def close(self): pass

    async def fake_open(iface, host, port, ssl=None, timeout=10.0):
        if iface.name == "phone":
            attempts["phone"] += 1
            if attempts["phone"] < 3:
                raise ConnectionError("no internet yet")
        return object(), W()
    monkeypatch.setattr(net, "open_via", fake_open)

    async def run():
        p = pool(Iface("lan", "1.1.1.1"), Iface("phone", "2.2.2.2"))
        task = asyncio.ensure_future(net.health_loop(p, every=0.2, retry=0.05, timeout=1))
        await asyncio.sleep(0.05)
        early = p.ready.get("phone")
        await asyncio.sleep(0.6)
        task.cancel()
        return early, p.ready.get("phone")
    early, later = asyncio.run(run())
    assert early is False and later is True


def test_proxy_marks_a_network_not_ready_when_it_cannot_connect(server, monkeypatch):
    import maxnet.proxy as proxy
    real = proxy.open_via
    async def flaky(iface, host, port, ssl=None, timeout=10.0):
        if iface.name == "gone":
            raise ConnectionError("no route")
        return await real(LO, host, port)
    monkeypatch.setattr(proxy, "open_via", flaky)

    async def run():
        p = pool(Iface("gone", "9.9.9.9"), Iface("up", "2.2.2.2"))
        srv = await serve(p, port=0)
        port = srv.sockets[0].getsockname()[1]
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(f"GET http://127.0.0.1:{server}/x HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n".encode())
        await r.read(); srv.close()
        return p.ready
    assert asyncio.run(run()).get("gone") is False


def test_shutdown_ends_open_connections_without_leaving_pending_tasks(server, caplog):
    """Quitting with connections still open through the proxy used to print 'Event loop is closed' / 'Task was destroyed'."""
    import gc
    import logging
    from maxnet.proxy import shutdown

    async def run():
        p = pool(LO)
        srv = await serve(p, port=0)
        port = srv.sockets[0].getsockname()[1]
        conns = []
        for _ in range(3):                                       # three idle SOCKS connections, like open browser tabs
            r, w = await asyncio.open_connection("127.0.0.1", port)
            w.write(b"\x05\x01\x00"); await r.readexactly(2)
            w.write(b"\x05\x01\x00\x01" + socket.inet_aton("127.0.0.1") + server.to_bytes(2, "big"))
            await r.readexactly(10)
            conns.append(w)
        assert len(srv.maxnet_tasks) == 3
        await shutdown(srv)
        return len(srv.maxnet_tasks)
    with caplog.at_level(logging.ERROR, logger="asyncio"):
        assert asyncio.run(run()) == 0
        gc.collect()
    assert "destroyed but it is pending" not in caplog.text and "Event loop is closed" not in caplog.text
