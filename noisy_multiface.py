#!/usr/bin/env python3
"""
noisy_multiface.py — cycle noisy traffic across 5 spoofed network interfaces.

Each invocation (fired by cron):
  1. picks the next interface in the rotation (slots 1..5, then repeat),
  2. optionally SKIPS (random chance) without advancing the rotation,
  3. brings that interface up with its spoofed MAC / hostname / DHCP identity
     and SSH-safe policy routing (see netmanage.py),
  4. runs the patched noisy bound to that interface's IP, with the identity's
     User-Agent, for a random duration,
  5. tears the interface down and advances the rotation.

The 5 slots (from config.json):
  1  wired, untagged        real identity (management link, no spoofing)
  2  wired, VLAN 3          Apple MacBook   <name>-mbp
  3  wired, VLAN 4          Intel/Windows   WIN-<name>
  4  wifi  SE-Labs (PSK)    iPhone          <name>-iphone
  5  wifi  Guest (OWE)      Samsung Galaxy  <name>-Galaxy-S24

Run as root (nmcli MAC cloning + ip rule/route need it).
"""

import fcntl
import json
import logging
import os
import random
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import identities
import netmanage

HERE = Path(__file__).resolve().parent
CONFIG_FILE = Path(os.environ.get("NOISY_CONFIG", HERE / "config.json"))
SECRETS_FILE = Path(os.environ.get("NOISY_SECRETS", HERE / "secrets.json"))
VENV_PY = Path(os.environ.get("NOISY_PY", HERE / "venv" / "bin" / "python"))
NOISY_DIR = Path(os.environ.get("NOISY_DIR", HERE / "noisy"))
LOG_FILE = Path(os.environ.get("NOISY_LOG", HERE / "noisy_multiface.log"))
CRAWL_LOG_DIR = Path(os.environ.get("NOISY_CRAWL_LOG_DIR", HERE / "logs"))
LOCK_FILE = Path(os.environ.get("NOISY_LOCK", HERE / "noisy_multiface.lock"))
STATE_DIR = HERE / "state"
ROTATION_FILE = STATE_DIR / "rotation.txt"


def load_secrets():
    """Load per-profile secrets (e.g. WiFi PSKs) from the gitignored secrets.json.

    Kept out of config.json so no credential is ever committed. Shape:
        {"psk": {"<profile name>": "<passphrase>"}}
    """
    if SECRETS_FILE.exists():
        return json.loads(SECRETS_FILE.read_text())
    return {}


def apply_secrets(profiles, secrets, log):
    """Overlay secrets onto profiles in memory (never written back to disk)."""
    psk_map = secrets.get("psk", {})
    for p in profiles:
        if p["name"] in psk_map:
            p["psk"] = psk_map[p["name"]]


def setup_logging():
    log = logging.getLogger("multiface")
    log.setLevel(logging.INFO)
    if not log.handlers:
        h = logging.FileHandler(LOG_FILE)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(h)
    return log


def acquire_lock():
    fh = open(LOCK_FILE, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def read_rotation():
    try:
        return int(ROTATION_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return 0


def write_rotation(value):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    ROTATION_FILE.write_text(str(value) + "\n")


def run_noisy(profile, ident, bind_ip, run_cfg, log):
    CRAWL_LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    crawl_log = CRAWL_LOG_DIR / f"crawl-{stamp}-{profile['name']}.log"

    # WiFi slots need to stay associated long enough for the Meter dashboard to
    # register the client, so enforce a minimum run length for them regardless of
    # the general run_min_sec.
    lo = run_cfg["run_min_sec"]
    if profile["kind"] == "wifi":
        lo = max(lo, int(run_cfg.get("wifi_min_run_sec", 330)))
    hi = max(run_cfg["run_max_sec"], lo)
    duration = random.randint(lo, hi)
    threads = random.randint(run_cfg["threads_min"], run_cfg["threads_max"])

    cmd = [
        str(VENV_PY), "noisy.py",
        "--timeout", str(duration),
        "--threads", str(threads),
        "--min_sleep", str(run_cfg["min_sleep"]),
        "--max_sleep", str(run_cfg["max_sleep"]),
        "--log", run_cfg["crawl_log_level"],
        "--logfile", str(crawl_log),
        "--local-addr", bind_ip,
    ]
    if profile.get("user_agent"):
        cmd += ["--user-agent", profile["user_agent"]]

    log.info("starting noisy: iface=%s bind=%s dur=%ds threads=%d host=%s ua=%s log=%s",
             profile["name"], bind_ip, duration, threads,
             ident["hostname"] if ident else "(real)",
             (profile.get("user_agent") or "(rotating pool)"), crawl_log)
    start = time.time()
    try:
        subprocess.run(cmd, cwd=str(NOISY_DIR), timeout=duration + 120, check=False)
    except subprocess.TimeoutExpired:
        log.warning("noisy exceeded hard timeout; terminated")
    elapsed = time.time() - start
    log.info("noisy finished after %.0f s", elapsed)

    # Safety net: if noisy exited early, keep a WiFi client associated for the rest
    # of the minimum dwell so the dashboard still registers it before teardown.
    if profile["kind"] == "wifi" and elapsed < lo:
        hold = lo - elapsed
        log.info("holding wifi association %.0fs more for dashboard registration", hold)
        time.sleep(hold)


def main():
    log = setup_logging()

    if os.geteuid() != 0:
        log.error("must run as root (nmcli MAC cloning + ip rule/route). Aborting.")
        print("noisy_multiface must run as root.", file=sys.stderr)
        sys.exit(1)
    if not VENV_PY.exists() or not (NOISY_DIR / "noisy.py").exists():
        log.error("venv or patched noisy.py missing — run install.sh first.")
        sys.exit(1)

    cfg = json.loads(CONFIG_FILE.read_text())
    net, run_cfg, profiles = cfg["net"], cfg["run"], cfg["profiles"]
    profiles = sorted(profiles, key=lambda p: p["slot"])
    apply_secrets(profiles, load_secrets(), log)

    lock = acquire_lock()
    if lock is None:
        log.info("another run in progress; exiting")
        return

    try:
        if random.random() > run_cfg["run_probability"]:
            log.info("skipping this invocation (probability roll); rotation unchanged")
            return

        counter = read_rotation()
        profile = profiles[counter % len(profiles)]
        log.info("=== rotation %d -> slot %d (%s) ===", counter, profile["slot"], profile["name"])

        ident = identities.get_identity(profile)
        state = None
        try:
            state = netmanage.setup(profile, ident, net, log)
            run_noisy(profile, ident, state["bind_ip"], run_cfg, log)
        except Exception as exc:
            log.exception("run failed for %s: %s", profile["name"], exc)
        finally:
            try:
                netmanage.teardown(state, log)
            except Exception as exc:
                log.exception("teardown failed for %s: %s", profile["name"], exc)
            # Advance the rotation whether or not the run succeeded, so a single
            # broken interface can't wedge the cycle.
            write_rotation(counter + 1)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


if __name__ == "__main__":
    main()
