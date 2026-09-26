# noisy-pi

Runs [madereddy/noisy](https://github.com/madereddy/noisy) — a crawler that generates
random HTTP/S traffic to mimic human browsing — on a Raspberry Pi in irregular bursts
via cron.

> **`multiface` branch** — this branch adds a multi-interface mode that cycles noisy
> traffic across five spoofed network identities (untagged wired, two tagged VLANs, and
> two WiFi SSIDs) for Meter SE-Labs client-fingerprint testing. The original
> single-interface wrapper (`noisy_random.py`) is still here and documented further down.

## Multi-interface mode (`noisy_multiface.py`)

Each cron firing advances a rotation through 5 "interfaces". After 5 runs all have been
used once, then it repeats:

| Slot | Interface        | VLAN / SSID                        | Spoofed as        | Hostname            |
|------|------------------|------------------------------------|-------------------|---------------------|
| 1    | wired `eth0`     | untagged                           | real Pi (none)    | —                   |
| 2    | wired `eth0.3`   | VLAN tag 3                         | Apple MacBook     | `<name>-mbp`        |
| 3    | wired `eth0.4`   | VLAN tag 4                         | Intel / Windows   | `WIN-<name>`        |
| 4    | wifi `wlan0`     | `1Meter-SE-Labs-Spoke-1` (WPA3-Transition → SAE) | iPhone | `<name>-iphone`     |
| 5    | wifi `wlan0`     | `1Meter-Spoke-1-Guest` (OWE)       | Samsung Galaxy S24| `<name>-Galaxy-S24` |

Before either WiFi slot takes over the radio, the wrapper **gracefully disconnects**
`wlan0` from whatever it's currently associated with (`nmcli device disconnect`).

For each spoofing slot the wrapper, via NetworkManager (`nmcli`):

- clones a **MAC** with the right vendor OUI (Apple / Intel / Samsung) so fingerprinting
  attributes it correctly — generated once and reused (`identities.json`);
- sets the **DHCP hostname** (option 12) and, for Windows, the vendor class (`MSFT 5.0`);
- forces a matching **HTTP User-Agent** in noisy via the `--user-agent` patch;
- brings the VLAN/WiFi connection up with DHCP, then installs **policy routing** so noisy
  (bound to that interface's IP via `--local-addr`) egresses that interface **without
  touching the system default route** — so your SSH/management path on untagged `eth0`
  stays up the whole time.

`<name>` is a random English first name, chosen once per identity and persisted.

### Requirements & assumptions

- **Raspberry Pi OS with NetworkManager** (`nmcli`) — the Bookworm default.
- Runs **as root** (MAC cloning + `ip rule`/`ip route`), so cron is installed under root.
- The Pi's switch port must **trunk VLANs 3 and 4** (tagged) with the untagged/native VLAN,
  and both WiFi SSIDs must be reachable.
- **Management is Raspberry Pi Connect**, which rides the *system default route* outbound
  to Raspberry Pi's relay servers — it isn't bound to an interface. The tool never changes
  the default route (spoofed leases are `ipv4.never-default`, and noisy egress is confined
  to per-interface policy routing), so Pi Connect stays up throughout. The one requirement:
  **untagged `eth0` must be the default route and have internet**. Slots 4/5 take `wlan0`
  over temporarily, which is fine as long as your default route (and thus Pi Connect) is on
  wired `eth0`, not WiFi.

### Install & run (on the Pi)

```bash
git clone -b multiface https://github.com/Francisco-1088/noisy-pi.git
cd noisy-pi
bash install.sh                 # clones + patches noisy, builds venv, checks nmcli
./venv/bin/python identities.py # generate & preview the spoofed identities
sudo ./venv/bin/python noisy_multiface.py   # test ONE rotation step
tail -f noisy_multiface.log
bash install_cron.sh            # install into root crontab (every 30 min)
```

### Config

Everything lives in [`config.json`](config.json): interface names, the 5 profiles
(VLAN ids, SSIDs, security, User-Agents, DHCP options), and the run knobs
(`run_probability`, jitter, duration, threads, sleeps, crawl log level). Edit and it
takes effect next run — no code changes needed. Identities and the rotation counter are
generated at runtime into `identities.json` / `state/` (both gitignored).

**Secrets** (WiFi PSKs) live in `secrets.json`, which is **gitignored and never
committed**. `config.json` keeps `psk: null`; the real value is overlaid at runtime from
`secrets.json`, keyed by profile name. `install.sh` seeds it from `secrets.example.json` —
fill in the real PSK there:

```json
{ "psk": { "wifi-selabs-iphone": "your-selabs-psk" } }
```

### Which sites / identity did a run use

- `noisy_multiface.log` — one line per run naming the slot, interface, MAC, hostname, UA,
  IP, and the per-run crawl log path.
- `logs/crawl-<ts>-<profile>.log` — noisy's own output for that run; `./sites.sh` lists
  the URLs attempted (see below).

---

## Single-interface mode (`noisy_random.py`)

The original wrapper. `noisy.py` normally runs forever; instead cron fires
`noisy_random.py` on a fixed cadence and makes each run look random.

## How it works

`noisy.py` normally runs forever. Instead of that, cron fires `noisy_random.py` on a
fixed cadence, and the wrapper makes each run look random:

1. **Skip chance** — rolls `RUN_PROBABILITY` (default 0.7); sometimes it does nothing.
2. **Jitter** — sleeps a random 0–10 min so start times drift off the cron grid.
3. **Random duration** — runs noisy for a random 5–30 min via noisy's `--timeout`.
4. **Random params** — randomizes thread count and request delays each run.
5. **Locking** — an flock lock file prevents overlapping runs.

## Install (on the Pi)

Copy this folder to the Pi, then:

```bash
cd noisy-pi
bash install.sh          # clones noisy, builds venv, installs deps
bash install_cron.sh     # adds the cron entry (every 30 min by default)
```

Test manually before trusting cron:

```bash
NOISY_JITTER_MAX_SEC=0 NOISY_RUN_MAX_SEC=60 ./venv/bin/python noisy_random.py
tail -f noisy_random.log
```

## Tuning

Edit the constants at the top of `noisy_random.py`, or set env vars in the cron line.
All knobs (with defaults):

| Env var                  | Default | Meaning                                   |
|--------------------------|---------|-------------------------------------------|
| `NOISY_RUN_PROBABILITY`  | 0.7     | Chance a cron invocation actually runs    |
| `NOISY_JITTER_MIN_SEC`   | 0       | Min pre-start delay                       |
| `NOISY_JITTER_MAX_SEC`   | 600     | Max pre-start delay                       |
| `NOISY_RUN_MIN_SEC`      | 300     | Min run length                            |
| `NOISY_RUN_MAX_SEC`      | 1800    | Max run length                            |
| `NOISY_THREADS_MIN`      | 5       | Min crawler threads (Pi-friendly)         |
| `NOISY_THREADS_MAX`      | 20      | Max crawler threads                       |
| `NOISY_MIN_SLEEP`        | 3.0     | Min per-request delay (s)                 |
| `NOISY_MAX_SLEEP`        | 20.0    | Max per-request delay (s)                 |
| `NOISY_CRAWL_LOG_LEVEL`  | debug   | Crawl log detail (debug=per-URL list)     |

Change the cron cadence by setting `CRON_EXPR` before running `install_cron.sh`,
e.g. `CRON_EXPR="0 * * * *" bash install_cron.sh` for hourly.

## Logs

- `noisy_random.log` — wrapper decisions (skip / jitter / start / stop). Each
  start line names the crawl log for that run.
- `logs/crawl-YYYYmmdd-HHMMSS.log` — one file per run with noisy's own output.

## Which sites were attempted in a run

noisy logs each URL it visits only at **debug** level, which the wrapper uses by
default (`NOISY_CRAWL_LOG_LEVEL=debug`), writing a separate log per run. To list
the sites:

```bash
./sites.sh                       # most recent run
./sites.sh logs/crawl-20260720-143000.log   # a specific run
```

Or grep a log directly:

```bash
grep 'Visiting' logs/crawl-20260720-143000.log
```

Set `NOISY_CRAWL_LOG_LEVEL=info` for stats-only (no per-URL list) or `warning`
to keep the logs near-empty. Debug logs can grow large, so prune old ones
periodically, e.g. `find logs -name 'crawl-*.log' -mtime +7 -delete`.

## Remove

```bash
crontab -l | grep -v '# noisy-random' | crontab -
```
