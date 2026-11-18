"""noisy-multiface for the Pi Script Manager: one noisy run as one of seven client identities.

Each job:
  1. picks a slot: the `slot` parameter, or with slot=next the next one in `slots` (the counter
     lives on the Pi in /var/lib/pimgr-noisy-multiface/rotation, so it survives reboots);
  2. brings that slot's interface up with its spoofed MAC, DHCP hostname and vendor class, plus
     policy routing that never touches the default route (netmanage.py);
  3. runs noisy bound to that interface's address with the device's User-Agent, as the
     unprivileged job user (noisy_runner.py);
  4. tears the interface down and, after a Wi-Fi slot, reconnects wlan0 to what it was on before.

Slots (VLAN IDs and SSIDs come from the job's parameters):
  1  eth0 untagged    the Pi itself, no spoofing
  2  eth0.<vlan_a>    Apple MacBook    <name>-mbp
  3  eth0.<vlan_b>    Intel/Windows    WIN-<name>       (DHCP vendor class MSFT 5.0)
  4  wlan0 ssid_sae   iPhone           <name>-iphone    (psk_security: wpa-psk or sae)
  5  wlan0 ssid_owe   Samsung Galaxy   <name>-Galaxy-S24 (guest_security: wpa-psk, sae or owe)

wpa-psk (the default) matches a hand-made NetworkManager profile: key-mgmt wpa-psk, PMF default.
It joins WPA2 and WPA3-Transition SSIDs. sae forces WPA3 with PMF required.
  6  wlan0 ssid_nac   eap_device       named after nac_user (802.1X, EAP-TTLS)
  7  wlan0 ssid_3pr   eap_device       named after tpr_user (802.1X, EAP-TTLS)

Slots 6 and 7 sign in with WPA/WPA2-Enterprise, EAP-TTLS, no server certificate check. The inner
method follows the device: macbook PAP, windows MSCHAPv2, galaxy and iphone GTC. The spoofed
MAC vendor, DHCP hostname and User-Agent follow it too, with the username as the device name.

Wi-Fi passphrases (slot 4, and slot 5 unless guest_security is owe) and 802.1X passwords are
never job parameters (parameters are stored and audited). They are read from
/etc/pimgr/noisy-multiface/secrets.json on the Pi, root-owned, mode 600, and never printed:
  {"psk": {"<ssid>": "<passphrase>"}, "eap": {"<username>": "<password>"}}

mode=check tests every slot instead of running noisy (check.py).

Cancel (SIGTERM from the agent) still tears down: the agent waits 5 s before SIGKILL, and a run
that was killed anyway is cleaned up at the start of the next one.
"""
import fcntl
import json
import logging
import os
import pwd
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE_DIR = Path("/var/lib/pimgr-noisy-multiface")
os.environ.setdefault("NOISY_IDENTITIES", str(STATE_DIR / "identities.json"))

import check  # noqa: E402
import identities  # noqa: E402  (reads NOISY_IDENTITIES at import)
import netmanage  # noqa: E402

SECRETS_FILE = Path("/etc/pimgr/noisy-multiface/secrets.json")
ROTATION_FILE = STATE_DIR / "rotation"
LOCK_FILE = Path("/run/lock/pimgr-noisy-multiface.lock")
NOISY = Path("/opt/pimgr-tools/noisy/noisy.py")
RUN_USER = "pi-agent"
WIFI_MIN_S = 330   # long enough for the Meter dashboard to register the client
ALL_SLOTS = [1, 2, 3, 4, 5, 6, 7]

NET = {
    "wired_iface": "eth0",
    "wifi_iface": "wlan0",
    # Tables 211-217, rule priorities 10001-10007. pi-setup.sh uses tables 200/201 (wlan0/eth1)
    # and priorities 1200-1301, so nothing overlaps.
    "route_table_base": 210,
    "route_rule_priority_base": 10000,
    "nmcli_up_timeout": 90,
    "wifi_connect_attempts": 4,
    "wifi_scan_wait": 8,
}

UA = {
    "mac": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    "win": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
    "iphone": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Mobile/15E148 Safari/604.1",
    "galaxy": "Mozilla/5.0 (Linux; Android 14; SM-S921B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
}


# 802.1X slots (6, 7): what the device pretends to be, and the inner method it uses.
EAP_DEVICES = {
    "macbook": {"oui_pool": "apple", "hostname_template": "{name}-mbp", "user_agent": UA["mac"],
                "phase2": "pap"},
    "windows": {"oui_pool": "intel", "hostname_template": "WIN-{name}", "user_agent": UA["win"],
                "phase2": "mschapv2", "dhcp_vendor_class": "MSFT 5.0", "dhcp_client_id": "mac"},
    "galaxy": {"oui_pool": "samsung", "hostname_template": "{name}-Galaxy-S24",
               "user_agent": UA["galaxy"], "phase2": "gtc"},
    "iphone": {"oui_pool": "apple", "hostname_template": "{name}-iphone", "user_agent": UA["iphone"],
               "phase2": "gtc"},
}


def eap_profile(slot, tag, ssid_param, user_param):
    device = param("eap_device", "macbook")
    spec = EAP_DEVICES[device]
    user = param(user_param)
    prof = {"slot": slot, "name": f"wifi-{tag}-{device}", "kind": "wifi", "ssid": param(ssid_param),
            "wifi_security": "wpa-eap", "psk": None, "spoof": True, "fixed_name": user,
            "eap": {"identity": user, "password": None, "phase2": spec["phase2"]},
            "user_param": user_param}
    prof.update({k: v for k, v in spec.items() if k != "phase2"})
    return prof


class Stop(Exception):
    """SIGTERM from the agent (cancel or timeout)."""


def param(name, default=""):
    return os.environ.get(f"PARAM_{name.upper()}", default)


def profiles():
    """The seven slots, in netmanage.py's profile format."""
    vlan_a, vlan_b = int(param("vlan_a", "3")), int(param("vlan_b", "4"))
    return {
        1: {"slot": 1, "name": "wired-untagged", "kind": "wired", "vlan": None, "spoof": False,
            "management": True, "user_agent": None},
        2: {"slot": 2, "name": "wired-vlan-a-macbook", "kind": "wired", "vlan": vlan_a, "spoof": True,
            "oui_pool": "apple", "hostname_template": "{name}-mbp", "user_agent": UA["mac"]},
        3: {"slot": 3, "name": "wired-vlan-b-intel", "kind": "wired", "vlan": vlan_b, "spoof": True,
            "oui_pool": "intel", "hostname_template": "WIN-{name}", "user_agent": UA["win"],
            "dhcp_vendor_class": "MSFT 5.0", "dhcp_client_id": "mac"},
        4: {"slot": 4, "name": "wifi-sae-iphone", "kind": "wifi", "ssid": param("ssid_sae"),
            "wifi_security": param("psk_security", "wpa-psk"), "psk": None, "spoof": True, "oui_pool": "apple",
            "hostname_template": "{name}-iphone", "user_agent": UA["iphone"]},
        5: {"slot": 5, "name": "wifi-owe-galaxy", "kind": "wifi", "ssid": param("ssid_owe"),
            "wifi_security": param("guest_security", "wpa-psk"), "wifi_hidden": param("owe_hidden", "1") == "1", "psk": None,
            "spoof": True, "oui_pool": "samsung", "hostname_template": "{name}-Galaxy-S24",
            "user_agent": UA["galaxy"]},
        6: eap_profile(6, "nac", "ssid_nac", "nac_user"),
        7: eap_profile(7, "3pr", "ssid_3pr", "tpr_user"),
    }


def parse_slots(text):
    """'1,2,3' or '1-5' or '2-3,5' -> [1, 2, 3]."""
    out = []
    for part in text.replace(" ", "").split(","):
        if not part:
            continue
        lo, _, hi = part.partition("-")
        try:
            rng = range(int(lo), int(hi or lo) + 1)
        except ValueError:
            raise SystemExit(f"slots: '{part}' is not a slot number or range")
        for n in rng:
            if n not in ALL_SLOTS:
                raise SystemExit(f"slots: {n} is not a slot (1-7)")
            if n not in out:
                out.append(n)
    if not out:
        raise SystemExit("slots: list at least one slot")
    return out


def pick_slot(log):
    """(slot, advance) where advance() moves the rotation on, for slot=next."""
    choice = param("slot", "next")
    if choice != "next":
        return int(choice), lambda: None
    cycle = parse_slots(param("slots", "1-7"))
    try:
        counter = int(ROTATION_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        counter = 0
    slot = cycle[counter % len(cycle)]
    log.info("rotation %d over slots %s -> slot %d", counter, cycle, slot)

    def advance():
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        ROTATION_FILE.write_text(f"{counter + 1}\n")
    return slot, advance


def load_secret(kind, key):
    """secrets.json[kind][key] from the root-only secrets file, or None.
    kind "psk" is keyed by SSID, "eap" by 802.1X username."""
    try:
        st = SECRETS_FILE.stat()
    except FileNotFoundError:
        return None
    if st.st_uid != 0 or st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise SystemExit(f"{SECRETS_FILE} must be owned by root with mode 600")
    data = json.loads(SECRETS_FILE.read_text())
    return (data.get(kind) or {}).get(key)


def dns_servers(iface, log):
    proc = netmanage.run(["nmcli", "-g", "IP4.DNS", "device", "show", iface], log, check=False, quiet=True)
    return " ".join(x.strip() for x in proc.stdout.replace("|", "\n").splitlines() if x.strip())


def crawl(profile, ident, state, log):
    """Run noisy_runner.py as the job user, bound to the slot's address. Returns its exit code."""
    duration = int(param("duration_s", "900"))
    if profile["kind"] == "wifi" and duration < WIFI_MIN_S:
        log.info("Wi-Fi slot: running %ds instead of %ds so the dashboard registers the client", WIFI_MIN_S, duration)
        duration = WIFI_MIN_S
    env = {k: v for k, v in os.environ.items() if k.startswith(("PARAM_", "PIMGR_")) or k in ("PATH", "LANG")}
    env.update(NOISY_DURATION=str(duration), NOISY_LABEL=f"slot {profile['slot']} {state['iface']}")
    if not profile.get("management"):
        env.update(NOISY_BIND_IP=state["bind_ip"], NOISY_DNS=dns_servers(state["iface"], log))
    if profile.get("user_agent"):
        env["NOISY_UA"] = profile["user_agent"]
    user = pwd.getpwnam(RUN_USER)
    workdir = Path(tempfile.mkdtemp(prefix="noisy-multiface-"))
    os.chown(workdir, user.pw_uid, user.pw_gid)
    try:
        proc = subprocess.Popen([os.environ["PIMGR_TOOLS_PY"], str(HERE / "noisy_runner.py"), str(NOISY)],
                                cwd=workdir, env=env, user=user.pw_uid, group=user.pw_gid, extra_groups=[])
        try:
            return proc.wait(timeout=duration + 120)
        except subprocess.TimeoutExpired:
            log.warning("noisy did not stop in time; killing it")
            proc.kill()
            return proc.wait()
        except Stop:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
            raise
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def main():
    logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    log = logging.getLogger("multiface")
    if os.geteuid() != 0:
        print("noisy-multiface must run as root (requires_root in manifest.json)", file=sys.stderr)
        return 2

    def on_term(*_):
        raise Stop()
    signal.signal(signal.SIGTERM, on_term)

    if param("mode", "run") != "check":
        check.save_run_params(STATE_DIR)   # before the lock, so even a run that overlaps a check saves
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another noisy-multiface run is still going on this Pi", file=sys.stderr)
        return 3

    if param("mode", "run") == "check":
        def out(line):
            print(line, flush=True)
        out(f"noisy-multiface check (fix={param('fix', '0') == '1'}, connect={param('connect', '1') == '1'})")
        if param("use_schedule_values", "1") == "1":
            check.use_saved_params(STATE_DIR, out)
        slots = parse_slots(param("slots", "1-7")) if param("slot", "next") == "next" else [int(param("slot"))]
        return check.run_check(profiles=profiles(), net=NET, slots=slots, secrets_file=SECRETS_FILE,
                               state_dir=STATE_DIR, out=out, log=log, fix=param("fix", "0") == "1",
                               connect=param("connect", "1") == "1")

    slot, advance = pick_slot(log)
    profile = profiles()[slot]
    if profile["kind"] == "wifi":
        if not profile["ssid"]:
            print(f"slot {slot} needs an SSID (ssid_sae / ssid_owe / ssid_nac / ssid_3pr)", file=sys.stderr)
            advance()
            return 2
        if profile["wifi_security"] == "wpa-eap":
            user = profile["eap"]["identity"]
            if not user:
                print(f"slot {slot} needs a username ({profile['user_param']})", file=sys.stderr)
                advance()
                return 2
            profile["eap"]["password"] = load_secret("eap", user)
            if not profile["eap"]["password"]:
                print(f"slot {slot}: no password for 802.1X user '{user}' in {SECRETS_FILE}. "
                      f'Add it on the Pi as {{"eap": {{"{user}": "..."}}}} (root, mode 600).',
                      file=sys.stderr)
                advance()
                return 2
        if profile["wifi_security"] in ("sae", "wpa-psk"):
            profile["psk"] = load_secret("psk", profile["ssid"])
            if not profile["psk"]:
                print(f"slot {slot}: no passphrase for SSID '{profile['ssid']}' in {SECRETS_FILE}. "
                      f'Add it on the Pi as {{"psk": {{"{profile["ssid"]}": "..."}}}} (root, mode 600).',
                      file=sys.stderr)
                advance()   # don't let one unconfigured slot stop the rotation
                return 2

    netmanage.cleanup_stale(NET, ALL_SLOTS, log)
    ident = identities.get_identity(profile)
    previous_wifi = netmanage.wifi_active_connection(NET["wifi_iface"], log) if profile["kind"] == "wifi" else ""
    state, code, start = None, 1, time.time()
    try:
        state = netmanage.setup(profile, ident, NET, log)
        code = crawl(profile, ident, state, log)
    except Stop:
        log.info("stopping: cancelled")
        code = 143
    except Exception as exc:
        print(f"slot {slot} ({profile['name']}) failed: {exc}", file=sys.stderr)
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)   # finish the teardown
        try:
            netmanage.teardown(state, log)
        except Exception as exc:
            print(f"teardown failed: {exc}", file=sys.stderr)
        if profile["kind"] == "wifi":
            netmanage.restore_wifi(previous_wifi, log)
        advance()   # even after a failure, so one broken slot can't wedge the rotation
    result = {"slot": slot, "identity": profile["name"], "iface": (state or {}).get("iface"),
              "address": (state or {}).get("bind_ip"), "slot_seconds": round(time.time() - start)}
    if ident:
        result.update(hostname=ident["hostname"], mac=ident["mac"])
    if profile["kind"] == "wifi":
        result["ssid"] = profile["ssid"]
    if profile.get("eap"):
        result["eap_user"] = profile["eap"]["identity"]
    elif profile.get("vlan"):
        result["vlan"] = profile["vlan"]
    print("PIMGR_RESULT " + json.dumps(result), flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
