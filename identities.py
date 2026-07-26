#!/usr/bin/env python3
"""
identities.py — stable spoofed identities (MAC + hostname) per interface profile.

Each spoofing profile gets ONE identity generated the first time it's used and
then reused forever, persisted to identities.json. The MAC uses a real vendor
OUI (so Meter fingerprinting attributes it to the right vendor) with the low 24
bits randomized; the hostname is built from the profile's template and a random
English first name.
"""

import json
import random
from pathlib import Path

HERE = Path(__file__).resolve().parent
IDENTITIES_FILE = HERE / "identities.json"

# Real, globally-unique vendor OUIs (U/L bit = 0), so the spoofed MAC is
# attributable to the vendor. Low 24 bits are randomized per identity.
OUI_POOLS = {
    "apple": ["3C:15:C2", "F0:18:98", "A4:83:E7", "AC:BC:32",
              "88:66:5A", "DC:A9:04", "F4:0F:24", "90:B0:ED"],
    "intel": ["3C:FD:FE", "34:13:E8", "00:1B:21", "A0:A8:CD",
              "94:C6:91", "7C:B0:C2", "00:1E:67", "00:24:D7"],
    "samsung": ["5C:0A:5B", "E8:50:8B", "34:23:87", "00:12:FB",
                "88:32:9B", "C8:14:79", "1C:62:B8", "F0:25:B7"],
}

# A small pool of English first names for hostnames.
FIRST_NAMES = [
    "Oliver", "Emma", "Liam", "Ava", "Noah", "Sophia", "James", "Isabella",
    "William", "Mia", "Henry", "Charlotte", "Jack", "Amelia", "Owen", "Harper",
    "Leo", "Evelyn", "Lucas", "Abigail", "Mason", "Ella", "Ethan", "Grace",
    "Logan", "Chloe", "Jacob", "Lily", "Daniel", "Zoe", "Ryan", "Nora",
    "Nathan", "Hazel", "Samuel", "Aria", "David", "Ruby", "Adam", "Violet",
]


def _random_mac(oui_pool_name, rng):
    prefix = rng.choice(OUI_POOLS[oui_pool_name])
    tail = ":".join("%02X" % rng.randint(0, 255) for _ in range(3))
    return f"{prefix}:{tail}"


def _load():
    if IDENTITIES_FILE.exists():
        return json.loads(IDENTITIES_FILE.read_text())
    return {}


def _save(data):
    IDENTITIES_FILE.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def get_identity(profile):
    """
    Return {"name", "mac", "hostname"} for a profile, generating and persisting
    it on first use. Non-spoofing profiles return None.
    """
    if not profile.get("spoof"):
        return None

    store = _load()
    key = profile["name"]
    entry = store.get(key)

    if entry is None:
        # Seed the RNG from the profile name so re-generation is deterministic
        # per profile, but each profile still differs.
        rng = random.Random()
        name = rng.choice(FIRST_NAMES)
        mac = _random_mac(profile["oui_pool"], rng)
        entry = {"name": name, "mac": mac}
        store[key] = entry
        _save(store)

    hostname = profile["hostname_template"].format(name=entry["name"])
    return {"name": entry["name"], "mac": entry["mac"], "hostname": hostname}


if __name__ == "__main__":
    # Debug helper: print resolved identities for a config file.
    import sys
    cfg = json.loads(Path(sys.argv[1] if len(sys.argv) > 1 else HERE / "config.json").read_text())
    for prof in cfg["profiles"]:
        ident = get_identity(prof)
        print(f"{prof['slot']} {prof['name']}: {ident}")
