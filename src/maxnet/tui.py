import time
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.widgets import Footer, Input, OptionList, Static
from textual.widgets.option_list import Option

from . import apps, netsetup, sysproxy
from .bench import bar, run_all
from .check import run_checks
from .download import download, safe_filename
from .meta import Meta, mask_ip, probe_all, shared_lines
from .net import Pool, list_interfaces
from .proxy import serve

PORT = 1080
BAR = 30
ACCENT = "#d19a66"
HELP = {
    "menu": "↑↓ choose · Enter select · o on/off · i show/hide IP · q quit",
    "nets": "Enter turns a network on/off · Esc back",
    "apps": "Enter attaches/detaches · takes effect the next time the app starts · Esc back",
    "dl": "Paste a link and press Enter · Esc cancel",
    "quit": "Enter to confirm",
    "setup": "Enter picks a port to set up · Esc back",
    "confirm": "Enter to confirm · Esc cancel",
}


class MaxNet(App):
    TITLE = "maxnet"
    CSS = """
    #status { padding: 1 2 0 2; }
    #cards { padding: 1 2 0 2; height: auto; }
    #notice { padding: 0 2; height: auto; }
    #help { padding: 1 2 0 2; color: $text-muted; }
    #url { margin: 0 2; display: none; }
    #menu { margin: 0 2; border: none; background: transparent; height: auto; max-height: 14; }
    """
    BINDINGS = [("o", "toggle_sys", "on/off"), ("escape", "back", "back"), ("i", "toggle_ip", "show/hide IP"), ("q", "ask_quit", "quit")]

    def __init__(self, system_proxy=True):
        super().__init__()
        self.system_proxy = system_proxy
        self.pool = Pool()
        self.last, self.rate = {}, {}
        self.peak = 1.0
        self.mode = "menu"
        self.busy = False
        self.show_ip = False
        self.proxy_ok = False
        self.auto_paused = False  # True while we turned ourselves OFF because every network disconnected
        self.pending = None
        self.results = []
        self.metas, self.tests = {}, {}

    def compose(self) -> ComposeResult:
        yield Static(id="status")
        yield Static(id="cards")
        yield Static(id="notice")
        yield Static(id="help")
        yield Input(placeholder="https://… link to a big file", id="url")
        yield OptionList(id="menu")
        yield Footer()

    async def on_mount(self):
        try:
            await serve(self.pool, port=PORT)
            self.proxy_ok = True
            if self.system_proxy and self.pool.live():
                sysproxy.enable(PORT)  # restored when you quit
        except OSError:
            self.say(f"Port {PORT} is already in use (is another maxnet open?). Dashboard works, but nothing is routed.", "red")
        self.set_interval(1, self.tick)
        self.set_interval(60, self.refresh_meta)
        self.refresh_meta()
        self.show_menu()
        self.tick()

    # ---------- display ----------
    def say(self, msg, style="bold"):
        self.query_one("#notice", Static).update(Text(msg, style=style))

    def refresh_meta(self):
        self.run_worker(self.meta_worker(), group="meta", exclusive=True)

    async def meta_worker(self):
        self.metas = await probe_all(list_interfaces())

    def tick(self):
        now = time.monotonic()
        ifaces = list_interfaces()
        for i in ifaces:
            total = self.pool.bytes.get(i.name, 0)
            t0, b0 = self.last.get(i.name, (now, total))
            self.rate[i.name] = (total - b0) / max(now - t0, 1e-6)
            self.last[i.name] = (now, total)
        self.peak = max(self.peak, *self.rate.values(), 1.0)
        live = [i for i in ifaces if i.name in self.pool.enabled]
        self.guard_system_proxy(live)
        agg = sum(self.rate.get(i.name, 0) for i in live) * 8 / 1e6

        st = Text()
        syson = sysproxy.active()
        if self.auto_paused:
            st.append("⚠ PAUSED  ", style="bold #e5c07b")
            st.append("no selected network is connected. Your Mac uses its normal settings and maxnet turns back on when one returns.", style="dim")
        elif syson:
            st.append("● ON  ", style="bold green")
            st.append(f"apps follow your macOS proxy settings and use {len(live)} network{'s' if len(live) != 1 else ''} together   ")
            st.append(f"{agg:.1f} Mbit/s now", style=f"bold {ACCENT}")
        else:
            st.append("○ OFF  ", style="bold")
            st.append(f"{len(live)} network{'s' if len(live) != 1 else ''} ready. Your Mac still uses its normal single connection. Press o to turn on.", style="dim")
        self.query_one("#status", Static).update(st)

        out = Text()
        for i in ifaces:
            on = i.name in self.pool.enabled
            m = self.metas.get(i.name, Meta())
            r = self.rate.get(i.name, 0) * 8 / 1e6
            out.append(f"{m.label or i.name}", style="bold")
            out.append(f"  ({i.name})  ", style="dim")
            out.append("● in use" if on else "○ not in use", style=ACCENT if on else "dim")
            out.append(f"   {self.pool.active.get(i.name, 0)} active downloads\n")
            out.append("  " + (bar(self.rate.get(i.name, 0) / self.peak, BAR) if on else "─" * BAR), style="green" if on else "dim")
            out.append(f"  {r:6.1f} Mbit/s   {self.pool.bytes.get(i.name, 0) / 1e6:,.0f} MB so far\n")
            out.append("  " + self.detail(i, m) + "\n", style="dim")
        for g in shared_lines(self.pool.enabled, self.metas):
            out.append(f"\nNote: {' and '.join(g)} share one internet line (same public IP). The gain depends on your router, so try the speed test.\n", style="#e5c07b")
        new = netsetup.unconfigured()
        if new:
            out.append(f"\nFound a network port macOS has not set up ({', '.join(new)}). Choose 'Set up new network port' in the menu.\n", style="#e5c07b")
        if not ifaces:
            out.append("No networks found. Connect Wi-Fi, Ethernet, or a phone over USB.")
        elif len(live) < 2:
            out.append("\nTip: use at least two networks (for example Wi-Fi + a phone over USB) to get more speed.\n", style="#e5c07b")
        self.query_one("#cards", Static).update(out)

    def guard_system_proxy(self, live):
        """Never leave the Mac pointing at a proxy with no network behind it."""
        if not (self.system_proxy and self.proxy_ok):
            return
        if not live and sysproxy.active():
            sysproxy.disable()
            self.auto_paused = True
            self.say("All selected networks disconnected. Turned OFF so your Mac keeps its normal connection.", "bold #e5c07b")
        elif live and self.auto_paused:
            self.auto_paused = False  # only resume what we paused; a manual OFF stays OFF
            if sysproxy.enable(PORT):
                self.say("A network is back. Turned ON again.", "bold green")

    def detail(self, i, m):
        if m.reachable is None:
            return "checking connection…"
        if not m.reachable:
            return "No internet on this network"
        bits = [f"ping {m.latency_ms:.0f} ms"]
        if m.link_mbit:
            bits.append(f"link {m.link_mbit} Mbit/s")
        if m.public_ip:
            bits.append(f"{m.public_ip if self.show_ip else mask_ip(m.public_ip)} · {m.isp} · {m.city}")
        if i.name in self.tests:
            bits.append(f"last test {self.tests[i.name]:.0f} Mbit/s")
        return "  ·  ".join(bits)

    # ---------- menus ----------
    def fill(self, mode, items):
        keep = (self.query_one("#menu", OptionList).highlighted or 0) if mode == self.mode else 0  # keep position only within one screen
        self.mode = mode
        self.query_one("#help", Static).update(HELP[mode])
        menu = self.query_one("#menu", OptionList)
        menu.clear_options()
        menu.add_options([Option(label, id=key) for key, label in items])
        menu.highlighted = min(keep, len(items) - 1)
        menu.focus()

    def show_menu(self):
        sysword = "Turn OFF (back to your normal connection)" if sysproxy.active() else "Turn ON"
        new = netsetup.unconfigured()
        extra = [("setup", f"Set up {len(new)} new network port{'s' if len(new) != 1 else ''} (phone just plugged in?)")] if new else []
        self.fill("menu", extra + [
            ("check", "Check that everything works"),
            ("test", "Speed test: each network, then all together"),
            ("dl", "Download a file using all networks"),
            ("nets", "Choose which networks to use"),
            ("apps", "Choose apps that use maxnet"),
            ("sys", sysword),
            ("quit", "Quit"),
        ])

    def show_setup(self):
        self.fill("setup", [(n, f"{n} has no address yet") for n in netsetup.unconfigured()] + [("back", "← Back")])

    def start_check(self):
        if self.busy:
            return self.say("Something is already running.")
        self.busy = True
        self.say("Checking…")
        self.run_worker(self.check_worker(), exclusive=True)

    async def check_worker(self):
        try:
            rows = await run_checks(self.pool, PORT, sysproxy.active(), True if self.system_proxy and self.proxy_ok else None)
            out = Text("Check results\n", style="bold")
            for ok, msg in rows:
                out.append(("✓ " if ok else "✗ "), style="bold green" if ok else "bold red")
                out.append(msg + "\n")
            self.query_one("#notice", Static).update(out)
        finally:
            self.busy = False

    def show_nets(self):
        items = []
        for i in list_interfaces():
            m = self.metas.get(i.name, Meta())
            on = "●" if i.name in self.pool.enabled else "○"
            items.append((i.name, f"{on} {m.label or i.name} ({i.name})"))
        self.fill("nets", items + [("back", "← Back")])

    def show_apps(self):
        on = apps.attached()
        items = [(n, f"{'●' if n in on else '○'} {n}") for n in apps.installed()]
        self.fill("apps", items + [("launch", "▶ Start attached apps through maxnet"), ("back", "← Back")])

    def on_option_list_option_selected(self, e):
        key = e.option.id
        if key == "back":
            return self.show_menu()
        if self.mode == "menu":
            return {"setup": self.show_setup, "check": self.start_check, "test": self.start_test, "dl": self.ask_url, "nets": self.show_nets, "apps": self.show_apps,
                    "sys": self.toggle_sys, "quit": self.action_ask_quit}[key]()
        if self.mode == "setup":
            self.pending = ("setup", key)
            return self.fill("confirm", [("yes", f"Yes: create a macOS network service for {key} (changes Network settings)"), ("back", "← Cancel")])
        if self.mode == "confirm":
            kind, arg = self.pending
            if key == "yes" and kind == "setup":
                name = netsetup.setup(arg)
                self.say(f"Created network service '{name}' with DHCP. It should get an address in a few seconds.", "bold green")
            elif key == "yes" and kind == "launch":
                self.say("\n".join(apps.launch(n, PORT) for n in arg))
            return self.show_menu()
        if self.mode == "nets":
            self.pool.toggle(key)
            self.tick()
            return self.show_nets()
        if self.mode == "apps":
            if key == "launch":
                names = apps.attached()
                self.pending = ("launch", names)
                return self.fill("confirm", [("yes", f"Yes: start {len(names)} app{'s' if len(names) != 1 else ''} through maxnet now"), ("back", "← Cancel")])
            apps.toggle(key)
            return self.show_apps()
        if self.mode == "quit":
            return self.exit() if key == "yes" else self.show_menu()

    def toggle_sys(self):
        if not self.system_proxy:
            self.say("System proxy changes are disabled (started with --no-system-proxy).")
            return
        self.auto_paused = False
        if sysproxy.active():
            sysproxy.disable()
            self.say("Turned OFF. Your original proxy settings are restored.")
        elif self.proxy_ok and sysproxy.enable(PORT):
            self.say("Turned ON. Apps that follow macOS proxy settings now use all your networks.")
        else:
            self.say("Can't turn it on here (needs macOS and a running proxy).", "red")
        self.tick()
        if self.mode == "menu":
            self.show_menu()

    action_toggle_sys = toggle_sys

    # ---------- speed test ----------
    def start_test(self):
        if self.busy:
            return self.say("Something is already running.")
        if not self.pool.live():
            return self.say("Choose at least one network first.", "red")
        self.busy, self.results = True, []
        self.show_results("Testing… about 10 seconds per network")
        self.run_worker(self.test_worker(), exclusive=True)

    async def test_worker(self):
        def report(names, per):
            self.results.append((names, sum(per.values()), per))
            if len(names) == 1:
                self.tests[names[0]] = per[names[0]]
            self.show_results("Testing…")
        try:
            await run_all(self.pool, report)
            singles = [t for n, t, _ in self.results if len(n) == 1]
            both = [t for n, t, _ in self.results if len(n) > 1]
            if singles and both:
                self.show_results(f"Done. All networks together are {both[0] / max(singles):.1f}x faster than your best single network.")
            else:
                self.show_results("Done. Add a second network to see the combined speed.")
        except Exception as e:  # shown to the user, not fatal
            self.show_results(f"Test failed: {e}", "red")
        finally:
            self.busy = False

    def show_results(self, title, style="bold"):
        out = Text(title + "\n", style=style)
        top = max((t for _, t, _ in self.results), default=1) or 1
        for names, total, _ in self.results:
            label = " + ".join(names) if len(names) > 1 else names[0]
            out.append(f"{label:<18}", style="bold")
            out.append(bar(total / top, BAR), style=ACCENT if len(names) > 1 else "green")
            out.append(f" {total:7.1f} Mbit/s\n")
        self.query_one("#notice", Static).update(out)

    # ---------- download ----------
    def ask_url(self):
        if self.busy:
            return self.say("Something is already running.")
        self.mode = "dl"
        self.query_one("#help", Static).update(HELP["dl"])
        box = self.query_one("#url", Input)
        box.display = True
        box.focus()

    def hide_url(self):
        box = self.query_one("#url", Input)
        box.value, box.display = "", False
        self.show_menu()

    def on_input_submitted(self, e):
        url = e.value.strip()
        self.hide_url()
        if url:
            self.busy = True
            self.run_worker(self.dl_worker(url), exclusive=True)

    async def dl_worker(self, url):
        name = safe_filename(url)
        dest, n = Path.home() / "Downloads" / name, 1
        while dest.exists():
            dest, n = dest.with_name(f"{Path(name).stem} ({n}){Path(name).suffix}"), n + 1
        done = [0]

        def progress(k):
            done[0] += k
            self.say(f"Downloading {name}: {done[0] / 1e6:,.0f} MB")
        try:
            total = await download(url, str(dest), self.pool, progress)
            self.say(f"Saved to {dest} ({total / 1e6:,.0f} MB)", "bold green")
        except Exception as e:  # shown to the user, not fatal
            self.say(f"Download failed: {e}", "red")
        finally:
            self.busy = False

    # ---------- keys ----------
    def action_toggle_ip(self):
        self.show_ip = not self.show_ip
        self.tick()

    def action_back(self):
        self.hide_url() if self.mode == "dl" else self.show_menu()

    def action_ask_quit(self):
        if not sysproxy.active():
            return self.exit()
        self.fill("quit", [("yes", "Turn OFF and quit"), ("no", "← Stay")])


def run(system_proxy=True):
    try:
        MaxNet(system_proxy).run()
    finally:
        sysproxy.disable()  # also covers Ctrl+C / errors; after a hard kill run `maxnet system off`
