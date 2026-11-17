# noisy-pi

Runs [madereddy/noisy](https://github.com/madereddy/noisy) — a crawler that generates
random HTTP/S traffic to mimic human browsing — on a Raspberry Pi in irregular bursts
via cron.

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

## Pi Script Manager

This repo is also a single-script *source* for the Pi Script Manager (`manifest.json` at the
root, script `noisy-web`). Each job runs noisy **once** for `duration_s` and reports pages
visited and unique sites. For runs on a timetable, with random delay and skips, use a schedule
in the Manager (Schedules page) instead of the cron setup above.

- `pimgr_run.sh` is the entrypoint; `pimgr_noisy.py` runs noisy with the job's parameters.
- Pick `iface`: `eth0` uses the default route; `wlan0` binds every HTTP connection and DNS lookup
  to wlan0's address (and its DHCP DNS servers), so the traffic looks like a Wi-Fi client.
- noisy itself and its Python packages come from the Manager's `traffic-gen` profile, pinned to
  an upstream commit and checksum-verified, instead of being cloned at install time.

### noisy-multiface (folder `multiface/`)

The multiface mode from the `multiface` branch, as a second Manager script. Each job is one slot:

| Slot | Interface | Spoofed as | Hostname |
|---|---|---|---|
| 1 | `eth0` untagged | the Pi itself | – |
| 2 | `eth0.<vlan_a>` (default VLAN 3) | Apple MacBook | `<name>-mbp` |
| 3 | `eth0.<vlan_b>` (default VLAN 4) | Intel / Windows | `WIN-<name>` |
| 4 | `wlan0` on `ssid_sae` (WPA3-SAE) | iPhone | `<name>-iphone` |
| 5 | `wlan0` on `ssid_owe` (OWE) | Samsung Galaxy S24 | `<name>-Galaxy-S24` |

- `slot=next` (the default) takes the next slot in `slots` (default `1-5`, e.g. `1-3,5` to skip
  one). The counter is kept on the Pi. To pin the order in the Manager instead, schedule it with
  *Take turns* lines `slot=1` … `slot=5`.
- Each slot keeps the same MAC and first name across runs: `/var/lib/pimgr-noisy-multiface/identities.json`.
- Policy routing uses tables 211–215 and rule priorities 10001–10005; the default route never
  changes, so the agent and the tailnet stay on eth0. After a Wi-Fi slot, wlan0 reconnects to what
  it was on before.
- Wi-Fi slots run at least 330 s, so the Meter dashboard registers the client.
- The crawler runs as the unprivileged job user; only the network setup runs as root.
- Cancel tears the slot down. A run that was killed is cleaned up at the start of the next one.

Set up once per Pi:
- The switch port must trunk `vlan_a` and `vlan_b` tagged, with the management VLAN untagged.
- Slot 4's passphrase lives only on the Pi, never in a job parameter. As root:

  ```bash
  sudo install -d -m 700 /etc/pimgr/noisy-multiface
  sudo sh -c 'umask 077; cat > /etc/pimgr/noisy-multiface/secrets.json' <<'JSON'
  {"psk": {"1Meter-SE-Labs-Spoke-1": "<passphrase>"}}
  JSON
  ```

  Keys are SSIDs. The script refuses the file unless it is root-owned with mode 600. Without it,
  slot 4 fails with a message and the rotation moves on.
- The Pi needs the `traffic-gen` (or `lab-full`) profile, for noisy and its Python packages.

Add it in the Manager as its own source (Admin › Script sources): this repo, branch `pimgr`,
folder `multiface`, and tick *Allow root scripts* for that source only. It then runs as
`<source>/noisy-multiface`. The `noisy-web` source stays as it is.

Known limit: the passphrase is passed to `nmcli` on its command line when the Wi-Fi connection is
created, so a local user could see it in the process list for a moment. The Pis have no
interactive users besides the role accounts.
