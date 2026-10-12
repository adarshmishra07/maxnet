# maxnet

Use every internet connection on your machine at the same time.

Plug in your phone over USB, keep Wi-Fi on, and maxnet spreads your traffic across all of them from one terminal screen. No hardware, no VPN, no server, no root.

*Illustrative layout, the numbers below are made up. See the measured result next.*

```
● ON  apps follow your macOS proxy settings and use 3 networks together   312.4 Mbit/s now

Wi-Fi  (en0)  ● in use   4 active downloads
  ━━━━━━━━━━━━━━━━━━━━──────────   71.2 Mbit/s   1,204 MB so far
  ping 17 ms  ·  203.•••.•••.••• · <your ISP> · <your city>
iPhone USB  (en9)  ● in use   5 active downloads
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  168.0 Mbit/s   2,980 MB so far
```

## Measured result (one machine, one run, not a promise)

| Setup | Speed |
|---|---|
| Browser on one connection (fast.com) | 140 Mbit/s |
| Same browser through maxnet, 3 connections (Wi-Fi, USB LAN, iPhone) | 340 Mbit/s |

The same test shows up in the macOS network counters: without maxnet, one interface carries everything; with maxnet, all three do. Your result depends on your connections, your router and the server you download from. Run `maxnet test` to measure your own.

## Install

Easiest, works on a fresh Mac or Linux machine (needs only `curl`, no Python or uv):

```bash
curl -LsSf https://raw.githubusercontent.com/adarshmishra07/maxnet/main/install.sh | sh
maxnet
```

The script installs [uv](https://docs.astral.sh/uv/) if you do not have it (uv brings its own Python), then installs maxnet. Read it first at [install.sh](install.sh) if you like.

Already have uv or pipx:

```bash
uv tool install https://github.com/adarshmishra07/maxnet/archive/refs/heads/main.tar.gz
```

PyPI (`uvx maxnet`, `pipx install maxnet`) is coming.

## Use

Run `maxnet`. It turns itself ON when it starts and asks before turning OFF when you quit.

| Key | Does |
|---|---|
| `o` | turn ON / OFF |
| `i` | show / hide public IPs (hidden by default) |
| `Esc` | go back |
| `q` | quit (asks first, then restores your proxy settings) |

From the menu you can run a speed test (each network alone, then together), download a file over all networks, choose which networks to use, and choose which apps use maxnet.

Command line, if you prefer:

```bash
maxnet list                    # networks, ping, ISP, which are in use
maxnet add en9                 # use a network (e.g. your phone)
maxnet remove en0
maxnet test                    # speed test: singles, then combined
maxnet dl <url>                # one big file split across all networks
maxnet apps                    # supported apps and which are attached
maxnet attach "Google Chrome"
maxnet open                    # start attached apps through maxnet
maxnet system off              # restore proxy settings (after a crash)
eval "$(maxnet env)"           # terminal tools (git, curl, npm) use it too
```

## How it works

Your OS sends all traffic out one default connection and leaves the others idle. maxnet runs a local proxy on `127.0.0.1:1080`. For every new connection the proxy picks the network with the fewest open connections and opens the socket pinned to that network (`IP_BOUND_IF` on macOS, `SO_BINDTODEVICE` on Linux). More connections means more networks working at once.

For single big files, `maxnet dl` splits the file into 8 MB blocks with HTTP Range requests and fetches them over every network in parallel, so one download gets the combined speed. The idea comes from [Plexo](https://github.com/anmolkapil/plexo).

On macOS, turning ON also points the system SOCKS proxy at maxnet, so apps that follow system proxy settings use it automatically. Your original settings are saved first and restored when you turn OFF or quit.

## Platform support

| | macOS | Linux |
|---|---|---|
| Local proxy, speed test, `maxnet dl`, TUI | yes | yes |
| System-wide ON/OFF (system proxy) | yes | not yet (use `eval "$(maxnet env)"` or point apps at `socks5h://127.0.0.1:1080`) |
| Attach apps (launch with proxy flag) | yes | not yet |

Windows is not supported.

## Limits (read these)

- **One connection stays on one network.** The speedup comes from many connections at once, which browsers and downloaders do. A single plain stream is not faster.
- **Not every app uses the proxy.** Apps that ignore system proxy settings keep using your normal connection. Supported Chromium apps (Chrome, Brave, Edge, Arc, Slack, Discord, VS Code and more) are all attached by default and are launched with a proxy flag; restart them once to pick it up, and detach any you do not want. Safari and Firefox follow system settings only.
- **Your networks must reach the internet separately.** Wi-Fi plus Ethernet on one router share one line, though some routers still give each device its own limit. Check with the speed test. A phone over USB is a separate line.
- **Android over USB on macOS** needs a driver ([TetherKit](https://github.com/XiaoMiku01/TetherKit)); iPhone works out of the box.
- IPv4 only. `maxnet dl` needs servers that support Range requests and cannot resume yet.
- Real multi-network speedups for every app, including single streams, need a server you run that merges the connections. maxnet does not do that.

## Safety and privacy

- Turning ON changes your macOS system proxy. Original settings are saved to the config file and restored on OFF, on quit, and by `maxnet system off` if maxnet is killed.
- `maxnet` looks up your public IP and ISP through ip-api.com (over plain HTTP, once a minute) to show line info and detect shared lines. Set `MAXNET_NO_LOOKUP=1` to skip it.
- Everything else stays on your machine. No telemetry.
- Found a security problem? Please use GitHub's private vulnerability reporting on this repo instead of a public issue.

## Code map

```
src/maxnet/
  net.py        interfaces, the Pool that picks one per connection, sockets pinned to an interface, config file
  proxy.py      local SOCKS5 + HTTP proxy; every connection leaves via Pool.pick()
  download.py   Plexo-style Range-block downloader over all networks
  bench.py      speed test: each network alone, then combined
  meta.py       per-network details: label, ping, public IP / ISP, default route, shared-line detection
  sysproxy.py   macOS system proxy on/off with backup and restore
  apps.py       attach/detach apps, launch with the proxy flag, shell env vars
  tui.py        the Textual interface
  cli.py        the `maxnet` command
tests/          run with: uv run pytest
```

## Develop

```bash
git clone https://github.com/adarshmishra07/maxnet && cd maxnet
uv sync
uv run pytest -q
uv run maxnet
```

macOS and Linux. MIT licensed.
