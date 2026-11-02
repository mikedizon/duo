# iPhone Duo pickup monitor

Watches Apple's **in-store pickup** availability for all eight iPhone Duo configurations at the Apple Stores near you, and tells you the moment one shows up. It plays a chime and opens the purchase page for the configurations you care about. Every check is saved in a local SQLite database, so you can see afterwards when stock appeared, at which stores, and whether it follows a pattern.

- It only **observes** pickup inventory. It never buys anything, and it doesn't track shipping or delivery.
- Standard-library Python 3.11+, macOS. There's nothing to install for the core monitor.
- Optional: run every check through a rotating **WireGuard VPN tunnel** (the WireGuard app, e.g. with a free Proton VPN account), with automatic switching when Apple starts refusing an IP.

> "Stock appeared" means Apple's pickup API reported it available at that moment. It is not a verified delivery time, and a quiet day doesn't prove nothing arrived.

---

## Contents

1. [Quick start](#quick-start)
2. [How it works](#how-it-works)
3. [Configuration](#configuration)
4. [Commands](#commands)
5. [Watching it run](#watching-it-run)
6. [Alerts, transitions and waves](#alerts-transitions-and-waves)
7. [Requests, pacing and refusals](#requests-pacing-and-refusals)
8. [VPN tunnels (optional)](#vpn-tunnels-optional)
9. [History and reports](#history-and-reports)
10. [Troubleshooting](#troubleshooting)
11. [What has been validated](#what-has-been-validated)
12. [Development](#development)

---

## Quick start

```sh
cd ~/Code/duo
cp .env.example .env                           # then edit it (next section)
python3 iphone_monitor.py --list-stores        # which stores will be watched
python3 iphone_monitor.py --dry-run            # test the chime and purchase page; no requests
python3 iphone_monitor.py --once               # one real check, prints the store × model grid
caffeinate -i python3 iphone_monitor.py --tui  # keep watching (Ctrl+C or q to stop)
```

**Choose your stores** in `.env` by ZIP code:

```sh
DUO_ZIPS=10001            # one or more 5-digit ZIPs, comma-separated
DUO_MAX_MILES=25          # watch the Apple Stores within this distance
DUO_ALERT_PARTS=NS1T,NS2T # which models open the purchase page (see below)
```

If `DUO_ZIPS` is unset, the monitor uses its built-in list of 18 Florida stores.

`caffeinate -i` keeps the Mac from going to sleep while idle. **Closing the lid on battery still puts it to sleep**, and polling stops; see [Troubleshooting](#troubleshooting).

---

## How it works

Each **poll** asks Apple, for every model at once, which nearby stores can do pickup today, and then works out what changed:

```
            ┌──────────────── every --interval seconds (default 20) ────────────────┐
            │                                                                       │
  .env ──▶ stores ──▶ 1 request per search ZIP ──▶ parse 12 nearest stores × 8 models │
 DUO_ZIPS  (cached)    (random nearby ZIP each       │                               │
                        time; optional VPN tunnel)   ▼                               │
                                      per (store, model): last confirmed status       │
                                                     │                               │
                                       change? ──▶ transition ──▶ wave (per store)    │
                                                     │                │              │
                                                     ▼                ▼              │
                                        purchase alert / chime    SQLite history ────┘
```

1. **Stores.** Apple's pickup endpoint answers a ZIP with its **12 nearest stores** and their distances. The stores within `DUO_MAX_MILES` of any of your ZIPs are watched. That list is looked up once and saved (refreshed after 7 days, or with `--refresh-stores`).
2. **Requests.** One request per search ZIP asks for all eight models. Each store is requested through the search ZIP it's nearest to.
   - Each poll the request uses a **random ZIP** from that search ZIP's list (see [`DUO_NEARBY_ZIPS_*`](#configuration)), never the same one twice in a row.
   - If an answer is missing one of your stores, that ZIP is dropped for the rest of the run, and the store is fetched once with its own ZIP.
3. **State.** Each (store, model) keeps its last *confirmed* status: available, unavailable or ineligible. A failed or unclear answer never overwrites it.
4. **Transitions.** A change between two confirmed statuses (e.g. unavailable → available) is a transition. It's recorded with how sure the timing is: `exact`, `after_gap` or `startup`.
5. **Waves.** Stock that appears at one store within `--wave-window` minutes (default 10) is grouped into one **restock wave**.
6. **Alerts.** A newly available model in `DUO_ALERT_PARTS` plays a chime and opens its Verizon purchase page, at most one tab per poll.
7. **History.** Everything goes to `inventory_history.sqlite3`, with each poll in one transaction.

### The eight models

| Code | Part | Model |
|---|---|---|
| NS256 | MK1F4LL/A | 256GB Night Sky |
| NS512 | MK1H4LL/A | 512GB Night Sky |
| NS1T | MK1L4LL/A | 1TB Night Sky |
| NS2T | MK1R4LL/A | 2TB Night Sky |
| SW256 | MK1E4LL/A | 256GB Star White |
| SW512 | MK1G4LL/A | 512GB Star White |
| SW1T | MK1J4LL/A | 1TB Star White |
| SW2T | MK1Q4LL/A | 2TB Star White |

Part numbers were taken from `apple.com/shop/buy-iphone/iphone-duo` and confirmed in pickup answers on 2026-10-27. The purchase pages are the Verizon ones.

---

## Configuration

Settings go in a `.env` file next to `iphone_monitor.py` (start from `.env.example`). Real environment variables override the file, and values are never logged.

| Variable | What it does | Default |
|---|---|---|
| `DUO_ZIPS` | Search ZIP code(s), comma-separated. One request per poll each. | unset: built-in 18 Florida stores |
| `DUO_MAX_MILES` | Watch the stores within this many miles of any search ZIP (more than 0, at most 500) | `25` |
| `DUO_NEARBY_ZIPS_<ZIP>` | Extra ZIPs that a search ZIP's request rotates through, e.g. `DUO_NEARBY_ZIPS_10001=10002,10011` | the search ZIP and its stores' own ZIPs within 15 mi (built-in Florida set: 12 fixed ZIPs per region) |
| `DUO_ALERT_PARTS` | Models that open the purchase page, highest priority first (`NS1T,NS2T`, …), or `all` | `NS1T,NS2T` |

**Checking the store list:** `python3 iphone_monitor.py --list-stores` shows each store with its ID, city, ZIP, distance and the search ZIP it's requested through. The first time it makes one request per search ZIP; after that it uses the saved list.

**Common errors:**
- An invalid ZIP or setting stops the program with a usage error.
- "No Apple Store within N mi" names the nearest store and its distance; raise `DUO_MAX_MILES`.

**How alert priority works:**
- The highest-priority model that is newly available opens its page.
- A lower-priority one opens only if no store currently has a higher-priority model; that page was already opened.
- Models not in `DUO_ALERT_PARTS` never open the purchase page.
- Only the 1TB and 2TB Night Sky purchase URLs were checked to load; the others follow the same pattern.

---

## Commands

| Command | What it does |
|---|---|
| `python3 iphone_monitor.py` | Watch and record (stop with Ctrl+C) |
| `--interval 30` | Seconds between polls (minimum 5; default 20) |
| `--tui` | Full-screen view, one collapsible line per poll ([details](#the-interactive-view---tui)) |
| `--once` | One check, print the grid, exit (exit code 0 if every cell was confirmed, 2 otherwise) |
| `--dry-run` | Pretend 1TB Night Sky appeared at the first store: tests the chime and the purchase tab. No requests, no history. |
| `--list-stores` | Show the watched stores, then exit |
| `--refresh-stores` | Look the stores up again instead of using the saved list |
| `--waves [--days N] [--store NAME]` | List restock waves |
| `--report [--days N] [--store NAME]` | Timing and pattern analysis per store |
| `--export-csv [DIR]` | Export every history table to CSV |
| `--wave-window MIN` | Minutes that later stock still joins the same wave (default 10) |
| `--db PATH` | History database (default `inventory_history.sqlite3` next to the script) |
| `--verbose` | Detailed `key=value` logs on stderr, or to `iphone_monitor.log` with `--tui` |
| `--vpn-enabled` | Poll only through a verified WireGuard tunnel ([VPN tunnels](#vpn-tunnels-optional)) |
| `--vpn-rotate-minutes MIN` / `--vpn-rotate-every-cycles N` | Switch tunnels on a schedule (pick one) |
| `--vpn-list` / `--vpn-test` / `--vpn-status` | List tunnels / connect, check and disconnect one / show the tunnel in use |

---

## Watching it run

### Plain output

Every poll prints the full grid of stores × models:

```
[2026-10-27 09:42:20 EDT] Poll #152

STORE                   NS256 NS512  NS1T  NS2T SW256 SW512  SW1T  SW2T
Waterside Shops           -     -     ✓     -     -     -     -     -
...
Available 1/144 | Newly available 1 | New waves 1 | Unknown/error cells 0 | Duration 1.52s | Requests 2 | Errors 0 (none) | Tunnel 47 (37.19.221.244, Miami, Florida, US)
09:42:20 EDT  Waterside Shops         1TB Night Sky      AVAILABLE  wave #12
```

- The symbols are `✓` available, `-` unavailable, `x` ineligible, and `?` unknown or error.
- Newly available cells are green in a terminal; set `NO_COLOR` to turn color off.
- The `Tunnel` part of the footer appears only with `--vpn-enabled`.

### The interactive view (`--tui`)

```sh
caffeinate -i python3 iphone_monitor.py --tui
```

The screen shows one line per poll, newest first:

```
▾ ★ #27 13:54:52  1/144  45 (37.19.221.240, …)  req 2  ok  NEW: Brickell City Centre 512GB Night Sky
    STORE   NS256 NS512 …
▸ #26 13:54:22  0/144  45 (37.19.221.240, …)  req 2  ok
  • 13:54:17  Selected tunnel: 45
▸ ★ #12 13:47:22  0/144  130 (72.251.220.28, …)  req 2  ok  NEW: Waterside Shops 1TB Night Sky  (sold out by 13:52:52)
```

- **`★` marks a poll that found new stock.** It opens automatically and stays open, in green, while any of that stock is still available. Once all of it is gone it folds itself and dims, with `(sold out by …)`.
- "Still available" is the last *confirmed* status, so a poll where that store failed doesn't count as sold out. If you open or close a poll yourself, it stays the way you left it.
- 541/403 polls are yellow, and other errors red. Tunnel switches, cooldowns and alerts get their own lines.
- **The bottom bar** shows polls, `in stock now`, the available count, tunnel, last 541 and time to the next poll.
- **Keys:** ↑/↓ or j/k move, PgUp/PgDn jump 10, g/G go to newest/oldest, Enter or Space open/close, e/c expand/collapse all, q or Ctrl+C quit.
- **On quit:** the current poll finishes, then the tunnel is torn down as usual and the last events are printed.
- The view only shows this session; older history is in the database.

---

## Alerts, transitions and waves

**Transitions** are classified by how certain their timing is:
- `exact`: the poll just before was a confirmed observation, with no gap in between. A gap is more than max(2.5 × interval, 60 s), or any failed result.
- `after_gap`: failed polls or downtime came in between, so the exact moment is unknown.
- `startup`: the first observation ever for that store and model.

**Alerts** fire once per (store, model) and re-arm only after a confirmed *unavailable*. After a restart, a preferred model that's still in stock alerts once more, since you may want the tab again.

**Waves** open on a store's first new availability. Later transitions at that store join it for `--wave-window` minutes.
- A model that simply stays available never opens another wave.
- Each wave records its members, first and last times, how many polls saw it, and whether gaps affected it.

| `event_class` | Meaning |
|---|---|
| `new_availability` | Every member had an `exact` transition |
| `uncertain_timing` | Some member appeared after failed polls or downtime |
| `present_at_startup` | Some member had no earlier state |

| `confidence` | Meaning |
|---|---|
| `complete` | `new_availability`, and the store answered every poll in the window |
| `partial_coverage` | `new_availability`, but some poll in the window had errors for that store |
| `uncertain` | Any other class |

---

## Requests, pacing and refusals

- **Pace:** `--interval` alone sets it. At the 20 s default with two search ZIPs that's 2 requests every 20 s, 360 an hour. That's the rate at which Apple answered **HTTP 541** after about 11 minutes on 2026-10-27, without a VPN.
- **Ordering:** polls never overlap, and none are fired to catch up after a delay. With several search ZIPs, the requests go out in random order. The HTTP timeout is 10 s, and the User-Agent honestly identifies the script.
- **541 or 403 (refused):**
  - The poll stops and nothing is sent for 5 minutes.
  - Then one probe request goes out. A refused probe waits 10, then 20, then 30 minutes (capped). A clean probe resumes normal polling.
  - A restart resumes a cooldown in progress.
  - With `--vpn-enabled`, a 541 switches tunnels instead (below).
- **429 / 5xx:** the delay doubles each time, up to 15 minutes, and `Retry-After` is honored up to 1 hour.
- **Network errors or bad JSON:** the same backoff starts after 3 failing polls in a row. One clean poll resets it.

---

## VPN tunnels (optional)

With `--vpn-enabled`, every poll goes out through a WireGuard tunnel, so Apple sees a VPN IP instead of yours. No sudo is needed.

### Setup

1. **Create a free Proton VPN account** at [protonvpn.com](https://protonvpn.com). The free plan is enough; this project was run on Proton's free servers.
2. **Download WireGuard configuration files** from your Proton account: sign in at [account.protonvpn.com](https://account.protonvpn.com) and find the WireGuard configuration downloads (look in the account's downloads section; the exact menu names weren't checked for this guide). Create one file per server you want; more servers means more IPs to rotate through when Apple refuses one.
   - Check each file has a real key: the `PrivateKey = ` line should be a long string, not `*****`. One file used here had `*****` and couldn't connect until it was downloaded again (2026-10-28). Why isn't known; saving the file right away when you create it is a reasonable precaution.
   - Proton's pages and free-plan limits can change. Their own help pages are the reference for the current steps.
3. Install the **WireGuard** app from the Mac App Store.
4. Import the `.conf` files with *Import Tunnel(s) from File*. Each one becomes a tunnel; the tunnel name doesn't matter. You can keep the downloaded originals in [`proton_configs/`](proton_configs/README.md): git ignores everything in it except its README, and the monitor doesn't read it.
5. Run `python3 iphone_monitor.py --vpn-list` to check that the tunnels are listed. Then run `--vpn-test`, which connects one, verifies it and disconnects.

Any other WireGuard provider works the same way; Proton's free plan is just the one used here.

**Every tunnel in the WireGuard app is used**, so keep only the ones meant for this there. Tunnels you add or remove while the monitor runs are picked up at the next tunnel switch, with no restart.

### What happens

- **Connecting.** The monitor starts a random tunnel (not the one it just used) with `scutil --nc start` and counts it usable only after all of these pass:
  - the tunnel reports Connected within 20 s;
  - traffic goes through a new `utun` interface;
  - the tunnel's DNS is active and resolves within 10 s;
  - a public-IP lookup through the tunnel (ipinfo.io, falling back to ipify; both are independent services) returns a valid IP different from your own.
- **Location.** The tunnel's public IP and location show in the footer, e.g. `Tunnel 47 (37.19.221.244, Miami, Florida, US)`. Locations come from ipinfo.io's database and are often the VPN provider's registered address.
- **Fail closed.** Polls only run through a verified tunnel, never through your own connection.
- **Dead tunnels.** A tunnel can stay "Connected" while passing no traffic. After any poll with network errors, the monitor makes a real request through the tunnel. If that fails, it switches tunnels before the next poll and avoids the dead one for the rest of the run.
- **541 → switch.** When Apple refuses the tunnel's IP, the monitor switches to a different tunnel for the next poll instead of cooling down.
  - IPs Apple refused earlier in the run are avoided.
  - If the new tunnel lands on an already-refused IP, it tries one more, then accepts the result.
  - After 3 switches in a row without a clean poll, it falls back to the normal cooldown.
  - 403 and 429 never switch.
- **Schedule (optional).** `--vpn-rotate-minutes N` or `--vpn-rotate-every-cycles N` switch tunnels between polls. A scheduled switch never resets a cooldown.
- **Reconnects.** If a reconnect fails, retries come after 30, 60 and 120 s; after that the monitor stops with exit code 3.

### Ownership and cleanup

- The monitor only starts and stops WireGuard-app tunnels, by UUID, and stops only a tunnel it started. It refuses to connect while you have one up yourself. Other VPNs (ProtonVPN app, Tailscale) are never touched.
- **On exit:**
  1. It stops its tunnel (allowing up to 45 s for a slow disconnect).
  2. It checks that the interface, routes and DNS are back to how they were, and that your own connection works.
  3. If any check fails, it stops with exit code 3 and prints the manual steps below.
- **After a crash** the tunnel stays up, because the WireGuard app runs it. The next start finds the unfinished session and shuts that tunnel down first.

**Manual recovery** if the network misbehaves:

```sh
scutil --nc list | grep com.wireguard     # a (Connected) line is a WireGuard-app tunnel
scutil --nc stop "88"                     # by name, or deactivate it in the WireGuard app
route -n get default                      # interface should be en0 (Wi-Fi) or your Ethernet
scutil --dns | head                       # nameservers should no longer be the VPN's (e.g. 10.2.0.1)
curl -s https://api.ipify.org             # your own IP again
```

### What was seen with Proton free servers (2026-10-28/29)

These are observations, not guarantees:
- At a poll every 30 s, Apple refused each tunnel IP after **7.5–15 minutes** (median about 13).
- Several servers connected but passed no traffic (44, 138, 142), and one never finished connecting (87). The dead-tunnel check and the connect timeout handle both.
- Different configs sometimes share an exit IP.

---

## History and reports

```sh
python3 iphone_monitor.py --waves --days 1             # what appeared in the last day
python3 iphone_monitor.py --report --days 7            # timing patterns per store
python3 iphone_monitor.py --report --store "Aventura"  # one store (name, ID, or unique part of a name)
python3 iphone_monitor.py --export-csv                 # every table to ./exports/<timestamp>/
```

These read the database and are safe to run while the monitor is running.

**The report**, per store, uses only `new_availability` waves for timing. It covers:
- polling coverage;
- wave count and start-time statistics;
- time-of-day and day-of-week histograms;
- **repeated timing windows** (5/10/15/30-minute windows holding waves on the most distinct dates). Each window is assessed as *insufficient*, *weak* or *repeated in observed data*, and the last needs at least 5 supporting dates covering at least 60% of the dates polled;
- day-to-day shifts;
- which models appear together, and in what order;
- 1TB/2TB Night Sky signals.

**The database** is SQLite (WAL mode). Times are stored in UTC and shown in America/New_York.

| Table | Contents |
|---|---|
| `polls` | Each poll: times, requests, errors, available count, run, tunnel session |
| `poll_store_results` | Per poll and store: `ok`, `partial`, `unknown` or `error` |
| `sku_observations` | Per poll, store and model, **only for statuses other than unavailable** (a missing row means confirmed unavailable) |
| `availability_events` | Every transition, with its timing class |
| `store_restock_waves`, `restock_wave_members` | Waves and their members |
| `monitor_runs` | One row per session |
| `vpn_sessions`, `vpn_events` | Each tunnel used (IP, location, outcome) and its lifecycle events |
| `store_sets`, `store_set_members` | Saved store lists for each `DUO_ZIPS` / `DUO_MAX_MILES` setting |

**Schema upgrades:**
- They run automatically, one transaction per step.
- Before upgrading a database that holds data, a backup `inventory_history.sqlite3.v<N>-backup-<timestamp>` is written next to it.
- A database from a newer version of the script is refused.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Gaps of 10–30 min overnight, then it resumes | The Mac slept. On 2026-10-29 the lid was closed on battery (`pmset -g log` showed `Clamshell Sleep`), and `caffeinate -i` doesn't prevent that. Keep the lid open, or stay on the charger and use `caffeinate -s`. |
| Lots of `HTTP 541/403` | Apple is refusing your IP. Poll less often (`--interval 60`), or use `--vpn-enabled` so it switches tunnels. |
| `Connection failed (dns_error)` on some tunnels | That VPN server isn't passing traffic. The monitor moves on by itself. Remove the tunnel from the WireGuard app if it keeps failing. |
| Exit code 3, "Network restoration incomplete" | A tunnel didn't shut down cleanly. Follow the manual recovery steps above; the next start re-checks. |
| "No Apple Store within N mi" | Raise `DUO_MAX_MILES`, or check `DUO_ZIPS`. |
| "No saved store list for DUO_ZIPS yet" | `--dry-run` and reports never make requests. Run `--list-stores` once. |
| The purchase tab didn't open | Only models in `DUO_ALERT_PARTS` open it. Check `--waves --days 1` for what appeared. |

---

## What has been validated

**Live, against Apple (2026-10-27 to 10-29):**
- The 8 part numbers.
- Batched 8-model requests.
- The 18 Florida stores, from two regional requests (33607, 33131).
- The 1TB/2TB Night Sky purchase URLs (HTTP 200).
- Monitoring runs with history written.
- `--dry-run`.
- Real stock seen overnight on 10-29: 256GB Night Sky, 256GB Star White and 512GB Night Sky, appearing and selling out within 30 s to 4 min. So Apple's "available" value is parsed correctly.

**Live, tunnels (2026-10-28):**
- WireGuard-app start/stop without sudo.
- `--vpn-test`.
- Crash recovery.
- 541 → tunnel switching, many times.
- Dead-tunnel detection and replacement.
- New tunnels picked up while running.
- Location from ipinfo.io: response format checked on 10-30.

**Not yet seen live:**
- `DUO_ZIPS` discovery outside Florida. It's tested against the recorded Miami and Tampa answers: 33131 at 25 mi gives Brickell City Centre, Miami Worldcenter, Lincoln Road, Dadeland, The Falls and Aventura.
- The 1TB/2TB Night Sky alert firing on real stock.
- Apple's exact refusal thresholds.
- Recurring timing patterns from real waves (the analysis is tested on synthetic data).

---

## Development

| Module | Role |
|---|---|
| `iphone_monitor.py` | CLI, fetching and parsing, the poll loop, display, alerts |
| `inventory_model.py` | Models, stores (built-in Florida set, `configure_stores`), time helpers |
| `stores_config.py` | `DUO_ZIPS` → stores, by asking Apple |
| `inventory_waves.py` | Status tracking, transitions, waves, alert suppression |
| `inventory_db.py` | SQLite schema, migrations, writes, reads |
| `inventory_report.py` | `--waves`, `--report`, `--export-csv` |
| `tui.py` | The `--tui` model and its curses layer |
| `app_env.py` | `.env` loader |
| `wg_macos.py`, `wg_manager.py`, `wg_errors.py` | WireGuard-app tunnels: platform, lifecycle, gate, errors |

```sh
python3 -m unittest discover -s tests -v       # 296 tests; no network, no scutil, no real tunnels
DUO_WG_LIVE=1 python3 tests/live_wireguard.py  # optional: one real tunnel, connect + disconnect + checks
```

The unit tests use recorded Apple answers (`tests/fixtures/`) and a fake host and tunnels. They never touch the network or your real database.
