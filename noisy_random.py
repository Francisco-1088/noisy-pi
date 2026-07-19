#!/usr/bin/env python3
"""
noisy_random.py — wrapper around madereddy/noisy for the Raspberry Pi.

Cron fires this on a fixed cadence (see install.sh). Each invocation:
  1. optionally SKIPS the run (random chance) so activity is irregular,
  2. sleeps a random JITTER so the start time drifts off the cron grid,
  3. runs noisy.py for a random DURATION with lightly randomized params,
  4. uses a lock file so two runs never overlap.

Everything is tunable via the constants below or matching env vars.
"""

import fcntl
import logging
import os
import random
import subprocess
import sys
import time
from pathlib import Path

# --- Paths -------------------------------------------------------------------
HERE = Path(__file__).resolve().parent
NOISY_DIR = Path(os.environ.get("NOISY_DIR", HERE / "noisy"))
VENV_PY = Path(os.environ.get("NOISY_PY", HERE / "venv" / "bin" / "python"))
LOG_FILE = Path(os.environ.get("NOISY_LOG", HERE / "noisy_random.log"))
NOISY_LOG_FILE = Path(os.environ.get("NOISY_CRAWL_LOG", HERE / "noisy_crawl.log"))
LOCK_FILE = Path(os.environ.get("NOISY_LOCK", HERE / "noisy_random.lock"))


def _envf(name, default):
    return float(os.environ.get(name, default))


def _envi(name, default):
    return int(os.environ.get(name, default))


# --- Behavior knobs ----------------------------------------------------------
# Chance (0.0-1.0) that any given cron invocation actually runs. Lower = sparser.
RUN_PROBABILITY = _envf("NOISY_RUN_PROBABILITY", 0.7)

# Random delay before starting, so runs don't line up with the cron grid.
JITTER_MIN_SEC = _envi("NOISY_JITTER_MIN_SEC", 0)
JITTER_MAX_SEC = _envi("NOISY_JITTER_MAX_SEC", 600)      # up to 10 min

# How long a single noisy run lasts (passed to noisy's --timeout).
RUN_MIN_SEC = _envi("NOISY_RUN_MIN_SEC", 300)            # 5 min
RUN_MAX_SEC = _envi("NOISY_RUN_MAX_SEC", 1800)           # 30 min

# Pi-friendly resource band; noisy defaults (50 threads) are heavy for a Pi.
THREADS_MIN = _envi("NOISY_THREADS_MIN", 5)
THREADS_MAX = _envi("NOISY_THREADS_MAX", 20)

# Per-request delay band (noisy picks a random delay in [min_sleep, max_sleep]).
MIN_SLEEP = _envf("NOISY_MIN_SLEEP", 3.0)
MAX_SLEEP = _envf("NOISY_MAX_SLEEP", 20.0)


def setup_logging():
    logging.basicConfig(
        filename=str(LOG_FILE),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def acquire_lock():
    """Return an open file handle holding an exclusive lock, or None if busy."""
    fh = open(LOCK_FILE, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def main():
    setup_logging()

    if not VENV_PY.exists():
        logging.error("python interpreter not found at %s (run install.sh?)", VENV_PY)
        sys.exit(1)
    if not (NOISY_DIR / "noisy.py").exists():
        logging.error("noisy.py not found in %s (run install.sh?)", NOISY_DIR)
        sys.exit(1)

    lock = acquire_lock()
    if lock is None:
        logging.info("another run is in progress; exiting")
        return

    try:
        if random.random() > RUN_PROBABILITY:
            logging.info("skipping this invocation (probability roll)")
            return

        jitter = random.randint(JITTER_MIN_SEC, JITTER_MAX_SEC)
        logging.info("jittering %d s before start", jitter)
        time.sleep(jitter)

        duration = random.randint(RUN_MIN_SEC, RUN_MAX_SEC)
        threads = random.randint(THREADS_MIN, THREADS_MAX)

        cmd = [
            str(VENV_PY),
            "noisy.py",
            "--timeout", str(duration),
            "--threads", str(threads),
            "--min_sleep", str(MIN_SLEEP),
            "--max_sleep", str(MAX_SLEEP),
            "--log", "warning",
            "--logfile", str(NOISY_LOG_FILE),
        ]
        logging.info("starting noisy for %d s with %d threads", duration, threads)
        start = time.time()
        # Give noisy a hard ceiling in case --timeout is ignored/hangs.
        try:
            subprocess.run(cmd, cwd=str(NOISY_DIR), timeout=duration + 120, check=False)
        except subprocess.TimeoutExpired:
            logging.warning("noisy exceeded hard timeout; it was terminated")
        logging.info("noisy finished after %.0f s", time.time() - start)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


if __name__ == "__main__":
    main()
