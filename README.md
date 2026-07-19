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

Change the cron cadence by setting `CRON_EXPR` before running `install_cron.sh`,
e.g. `CRON_EXPR="0 * * * *" bash install_cron.sh` for hourly.

## Logs

- `noisy_random.log` — wrapper decisions (skip / jitter / start / stop).
- `noisy_crawl.log`  — noisy's own crawl output.

## Remove

```bash
crontab -l | grep -v '# noisy-random' | crontab -
```
