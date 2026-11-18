"""Runs madereddy/noisy once for the Pi Script Manager and reports a PIMGR_RESULT line.

Reads the job's PARAM_* variables. When NOISY_BIND_IP is set (a policy-routed test interface such
as wlan0), every HTTP connection and DNS lookup is sourced from that address, so the Pi's policy
rules send it out that interface, and DNS goes to that interface's servers (NOISY_DNS).
"""
import importlib.util
import json
import logging
import os
import signal
import socket
import sys
import time
from urllib.parse import urlsplit

import aiohttp
import aiohttp.client
from aiohttp.resolver import AsyncResolver


def env(name, default=""):
    return os.environ.get(f"PARAM_{name.upper()}", default)


def load_noisy(path):
    spec = importlib.util.spec_from_file_location("noisy", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def bind(noisy, ip, dns):
    """Source all crawler and DNS traffic from `ip` (so policy routing picks its interface)."""
    servers = dns or ["1.1.1.1"]
    if not dns:
        logging.warning("no DNS servers found for the interface; using 1.1.1.1 through it")

    def resolver():
        return AsyncResolver(nameservers=servers, local_ip=ip)

    base = aiohttp.TCPConnector

    class BoundConnector(base):
        def __init__(self, *args, **kwargs):
            kwargs.setdefault("local_addr", (ip, 0))
            kwargs.setdefault("family", socket.AF_INET)
            kwargs.setdefault("resolver", resolver())
            super().__init__(*args, **kwargs)

    aiohttp.TCPConnector = BoundConnector          # crawler sessions (noisy uses aiohttp.TCPConnector)
    aiohttp.client.TCPConnector = BoundConnector   # sessions created without a connector

    async def dns_noise_worker(domains, stop_event, min_sleep=noisy.DEFAULT_DNS_MIN_SLEEP,
                               max_sleep=noisy.DEFAULT_DNS_MAX_SLEEP, worker_id=0):
        import asyncio
        import random
        rng, res = random.Random(), resolver()
        logging.info("DNS noise worker %d started via %s (%d candidate domains)", worker_id, ip, len(domains))
        try:
            while not stop_event.is_set():
                host = rng.choice(domains)
                try:
                    await res.resolve(host, 0, socket.AF_INET)
                    logging.debug("DNS-only: resolved %s", host)
                except Exception as exc:  # NXDOMAIN, timeouts: part of the noise
                    logging.debug("DNS-only: %s for %s", type(exc).__name__, host)
                await asyncio.sleep(rng.uniform(min_sleep, max_sleep))
        finally:
            await res.close()

    noisy.dns_noise_worker = dns_noise_worker


def skip_ua_download(noisy):
    """Use noisy's built-in User-Agent list. useragents.me changed its page, so noisy's
    download fails on every run and logs a traceback; nothing is lost by not trying."""
    async def no_fetch(*_args, **_kwargs):
        return []

    noisy.fetch_user_agents = no_fetch


class Tally(logging.Handler):
    """Counts pages and sites from noisy's own debug log lines."""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.pages, self.sites, self.dns = 0, set(), 0

    def emit(self, record):
        msg = record.msg if isinstance(record.msg, str) else ""
        if "Visiting %s" in msg and len(record.args or ()) >= 2:
            self.pages += 1
            host = urlsplit(str(record.args[1])).hostname
            if host:
                self.sites.add(host)
        elif msg.startswith("DNS-only: resolved"):
            self.dns += 1


def main():
    noisy = load_noisy(sys.argv[1])
    tally = Tally()
    show_urls = env("show_urls") == "1"

    def setup_logging(_level, _logfile=None):
        root = logging.getLogger()
        root.handlers.clear()
        root.setLevel(logging.DEBUG)
        out = logging.StreamHandler(sys.stdout)
        out.setLevel(logging.DEBUG if show_urls else logging.INFO)
        out.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
        out.addFilter(lambda r: r.levelno > logging.DEBUG or "Visiting" in str(r.msg))
        root.addHandler(out)
        root.addHandler(tally)
        logging.getLogger("aiohttp").setLevel(logging.ERROR)

    noisy.setup_logging = setup_logging
    skip_ua_download(noisy)
    ip = os.environ.get("NOISY_BIND_IP")
    if ip:
        bind(noisy, ip, os.environ.get("NOISY_DNS", "").split())

    duration = int(env("duration_s", "600"))
    sys.argv = ["noisy", "--log", "debug", "--timeout", str(duration),
                "--num_users", env("users", "3"), "--threads", env("threads", "5"),
                "--min_sleep", env("min_sleep", "3"), "--max_sleep", env("max_sleep", "20"),
                "--dns_workers", env("dns_workers", "2"), "--crux_count", env("sites", "2000")]
    # Cancel from the manager sends SIGTERM: stop like Ctrl-C so the result still gets reported.
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt))
    print(f"noisy on {env('iface')} ({ip or 'default route'}) for {duration}s, "
          f"{env('users')} users x {env('threads')} threads", flush=True)
    start = time.time()
    try:
        noisy.main()
    except KeyboardInterrupt:
        pass
    result = {"iface": env("iface"), "seconds": round(time.time() - start), "pages_visited": tally.pages,
              "unique_sites": len(tally.sites), "dns_only_lookups": tally.dns}
    print("PIMGR_RESULT " + json.dumps(result), flush=True)
    return 0 if tally.pages or duration < 60 else 1


if __name__ == "__main__":
    sys.exit(main())
