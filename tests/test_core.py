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

    async def slow(iface, host, port, ssl=None):
        await asyncio.sleep(0.1)          # widen the window between pick() and connect finishing
        return await real(LO, host, port)  # fake ifaces can't be pinned, use loopback
    monkeypatch.setattr(proxy, "open_via", slow)
    ifaces = [Iface("a", "1.1.1.1"), Iface("b", "2.2.2.2"), Iface("c", "3.3.3.3")]

    async def run():
        p = pool(*ifaces)
        picked = []
        real_pick = p.pick
        p.pick = lambda: (picked.append(real_pick()) or picked[-1])
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
