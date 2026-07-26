#!/usr/bin/env python3
"""
apply_patch.py — patch a cloned upstream noisy.py with the multiface extensions.

Adds two CLI options to noisy:
  --user-agent UA    force a single User-Agent for every request (and skip the
                     rotating UA pool + its periodic refresh)
  --local-addr IP    bind all outgoing sockets to this source IP, so traffic
                     egresses a chosen interface via policy routing

The patch is applied with string-anchored replacements (not line numbers) and is
idempotent: running it twice is a no-op. If an anchor is missing (upstream drift),
it fails loudly so we don't silently ship a half-patched crawler.

Usage:  python apply_patch.py /path/to/noisy/noisy.py
"""

import sys
from pathlib import Path

MARKER = "multiface patch"

# (anchor, replacement). Each anchor must occur exactly once.
EDITS = [
    # A) module-level globals holding the overrides
    (
        "ua_pool = UAPool(_UA_FALLBACK)\n",
        "ua_pool = UAPool(_UA_FALLBACK)\n"
        "\n"
        "# --- multiface patch: injected overrides (set from CLI in main()) ---\n"
        "FORCED_UA = None   # --user-agent: single UA for every request\n"
        "LOCAL_ADDR = None  # --local-addr: source IP to bind outgoing sockets to\n"
        "# --- end multiface patch ---\n",
    ),
    # B) bind the connector to the chosen source IP
    (
        "            ttl_dns_cache=DNS_CACHE_TTL,\n"
        "            keepalive_timeout=keepalive_timeout,\n"
        "        )\n",
        "            ttl_dns_cache=DNS_CACHE_TTL,\n"
        "            keepalive_timeout=keepalive_timeout,\n"
        "            local_addr=(LOCAL_ADDR, 0) if LOCAL_ADDR else None,  # multiface patch\n"
        "        )\n",
    ),
    # C) skip the initial UA fetch when a UA is forced
    (
        "        initial_uas = await fetch_user_agents(session)\n"
        "        if initial_uas:\n"
        "            await ua_pool.replace(initial_uas)\n",
        "        if not FORCED_UA:  # multiface patch\n"
        "            initial_uas = await fetch_user_agents(session)\n"
        "            if initial_uas:\n"
        "                await ua_pool.replace(initial_uas)\n",
    ),
    # D) force every virtual user onto the single UA
    (
        "    ua_list = ua_pool.sample(num_users)\n",
        "    ua_list = ([FORCED_UA] * num_users) if FORCED_UA "
        "else ua_pool.sample(num_users)  # multiface patch\n",
    ),
    # E) don't run the periodic UA-refresh loop when a UA is forced
    (
        "    tasks.append(asyncio.create_task("
        "refresh_user_agents_loop(stop_event, ua_refresh_seconds, crawlers)))\n",
        "    if not FORCED_UA:  # multiface patch\n"
        "        tasks.append(asyncio.create_task("
        "refresh_user_agents_loop(stop_event, ua_refresh_seconds, crawlers)))\n",
    ),
    # F) register the new CLI options and push them into the globals
    (
        "    args = parser.parse_args()\n",
        '    parser.add_argument("--user-agent", dest="user_agent", default=None,\n'
        '                        help="Force a single User-Agent for all requests "\n'
        '                             "(multiface patch)")\n'
        '    parser.add_argument("--local-addr", dest="local_addr", default=None,\n'
        '                        help="Bind outgoing sockets to this source IP "\n'
        '                             "(multiface patch)")\n'
        "    args = parser.parse_args()\n"
        "    global FORCED_UA, LOCAL_ADDR  # multiface patch\n"
        "    FORCED_UA = args.user_agent\n"
        "    LOCAL_ADDR = args.local_addr\n",
    ),
]


def main():
    if len(sys.argv) != 2:
        print("usage: apply_patch.py /path/to/noisy.py", file=sys.stderr)
        sys.exit(2)

    path = Path(sys.argv[1])
    src = path.read_text()

    if MARKER in src:
        print(f"noisy.py already patched ({MARKER} present) — nothing to do.")
        return

    for i, (anchor, replacement) in enumerate(EDITS):
        count = src.count(anchor)
        if count != 1:
            print(
                f"ERROR: edit {i} anchor found {count} times (expected 1). "
                "Upstream noisy.py may have changed; patch not applied.",
                file=sys.stderr,
            )
            sys.exit(1)
        src = src.replace(anchor, replacement, 1)

    path.write_text(src)
    print(f"Patched {path} with multiface extensions (--user-agent, --local-addr).")


if __name__ == "__main__":
    main()
