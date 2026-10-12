import argparse
import asyncio
import sys

from .net import DEFAULT_PORT, Pool, list_interfaces


def _names():
    return {i.name for i in list_interfaces()}


def _set(name, on):
    pool = Pool()
    if name not in _names():
        sys.exit(f"{name}: not an up IPv4 interface. See `maxnet list`.")
    if (name in pool.enabled) != on:
        pool.toggle(name)
    print(f"{name}: {'added' if on else 'removed'}")


def _dl(url, out):
    from .download import safe_filename, unique_path
    pool = Pool()
    out = str(unique_path(out or safe_filename(url)))
    done = [0]

    def progress(n):
        done[0] += n
        print(f"\r{done[0] / 1e6:8.1f} MB", end="", flush=True)

    total = asyncio.run(_timed(url, out, pool, progress))
    print(f"\n{out}: {total / 1e6:.1f} MB")
    for i in pool.live():
        print(f"  {i.name:8} {pool.bytes.get(i.name, 0) / 1e6:8.1f} MB")


async def _timed(url, out, pool, progress):
    from .download import download
    return await download(url, out, pool, progress, note=lambda m: print(f"\n{m}", file=sys.stderr))


def _apps(a):
    from . import apps
    port = DEFAULT_PORT
    if a.cmd == "env":
        print(apps.env_exports(port))
    elif a.cmd == "apps":
        on = apps.attached()
        for n in apps.installed():
            print(f"{'*' if n in on else ' '} {n}")
        print("\nShell tools: add  eval \"$(maxnet env)\"  to ~/.zshrc (only active while maxnet runs)")
    elif a.cmd == "open":
        names = a.names or apps.attached()
        if not a.names and len(names) > 3 and input(f"Start {len(names)} apps through maxnet? [y/N] ").strip().lower() != "y":
            return
        for n in names:
            print(apps.launch(n, port))
    else:
        if a.name not in apps.installed():
            sys.exit(f"{a.name}: not an installed supported app. See `maxnet apps`.")
        if (a.name in apps.attached()) != (a.cmd == "attach"):
            apps.toggle(a.name)
        print(f"{a.name}: {'attached' if a.cmd == 'attach' else 'detached'}")


def _test(url, secs):
    from .bench import URL, bar, run_all
    pool = Pool()
    rows = []

    def report(names, per):
        total = sum(per.values())
        rows.append((names, total))
        print(f"{'+'.join(names):16} {total:7.1f} Mbit/s   " + "  ".join(f"{n} {v:.1f}" for n, v in per.items()), flush=True)

    try:
        asyncio.run(run_all(pool, report, url or URL, secs))
    except RuntimeError as e:
        sys.exit(str(e))
    from .bench import slow_warnings
    warn = slow_warnings({n[0]: t for n, t in rows if len(n) == 1})
    top = max((t for _, t in rows), default=1) or 1
    print()
    for names, total in rows:
        print(f"{'+'.join(names):16} {bar(total / top)} {total:.1f}")
    for w in warn:
        print(f"! {w}")


def main():
    p = argparse.ArgumentParser(prog="maxnet", description="Use every internet connection at once.")
    from . import __version__
    p.add_argument("--version", action="version", version=f"maxnet {__version__}")
    p.add_argument("--no-system-proxy", action="store_true", help="do not change the macOS system proxy when the TUI starts")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("list", help="show interfaces and which are in the pool").add_argument("--show-ip", action="store_true")
    sub.add_parser("add", help="add an interface to the pool").add_argument("name")
    sub.add_parser("remove", help="remove an interface from the pool").add_argument("name")
    pr = sub.add_parser("proxy", help="run the proxy without the TUI")
    pr.add_argument("--port", type=int, default=DEFAULT_PORT)
    d = sub.add_parser("dl", help="download one file over all enabled networks")
    d.add_argument("url")
    d.add_argument("-o", "--output")
    sub.add_parser("apps", help="list installed apps and which are attached")
    sub.add_parser("attach", help="attach an app to maxnet").add_argument("name")
    sub.add_parser("detach", help="detach an app from maxnet").add_argument("name")
    sub.add_parser("open", help="launch attached apps through maxnet (or the named ones)").add_argument("names", nargs="*")
    sub.add_parser("env", help='print proxy env vars for your shell: eval "$(maxnet env)"')
    sub.add_parser("system", help="macOS system proxy: on | off | status (off restores your original settings)").add_argument("state", choices=["on", "off", "status"])
    t = sub.add_parser("test", help="speed test: each network alone, then combined")
    t.add_argument("--secs", type=float, default=10)
    t.add_argument("--url")
    sub.add_parser("tui", help="interactive dashboard (default)")
    a = p.parse_args()

    if a.cmd == "list":
        from .meta import mask_ip, probe_all, shared_lines
        enabled, ifaces = Pool().enabled, list_interfaces()
        metas = asyncio.run(probe_all(ifaces))
        for i in ifaces:
            m = metas[i.name]
            ping = f"{m.latency_ms:.0f} ms" if m.reachable else "NO INTERNET"
            print(f"{'*' if i.name in enabled else ' '} {i.name:6} {i.ip:15} {m.label}{' (default)' if m.is_default else ''} · "
                  f"link {m.link_mbit} Mbit/s · ping {ping} · {m.public_ip if a.show_ip else mask_ip(m.public_ip)} {m.isp} {m.city}")
        for g in shared_lines(enabled, metas):
            print(f"! {' + '.join(g)} have the same public IP (one line); real gain depends on your router, check with `maxnet test`")
    elif a.cmd in ("add", "remove"):
        _set(a.name, a.cmd == "add")
    elif a.cmd == "system":
        from . import sysproxy
        if a.state == "on":
            from . import apps
            if not apps.proxy_up(DEFAULT_PORT):  # pointing the system at a dead proxy would cut every app's internet
                sys.exit(f"No maxnet proxy is listening on :{DEFAULT_PORT}. Run `maxnet` first (it turns the system proxy on by itself).")
            print("system proxy on" if sysproxy.enable(DEFAULT_PORT) else "macOS only")
        elif a.state == "off":
            print("restored your original proxy settings" if sysproxy.disable() else "nothing to restore")
        else:
            print("on (maxnet)" if sysproxy.active() else "off")
    elif a.cmd in ("apps", "attach", "detach", "open", "env"):
        _apps(a)
    elif a.cmd == "test":
        _test(a.url, a.secs)
    elif a.cmd == "dl":
        _dl(a.url, a.output)
    elif a.cmd == "proxy":
        from .net import health_loop
        from .proxy import serve, shutdown

        async def run():
            pool = Pool()
            server = await serve(pool, port=a.port)
            asyncio.ensure_future(health_loop(pool))
            print(f"proxy on 127.0.0.1:{a.port} (SOCKS5 + HTTP)")
            try:
                await asyncio.Event().wait()
            finally:
                await shutdown(server)
        asyncio.run(run())
    else:
        from .tui import run
        run(system_proxy=not a.no_system_proxy)
