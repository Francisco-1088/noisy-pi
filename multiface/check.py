"""noisy-multiface check mode (mode=check): what is this Pi missing for each slot, and why does a
slot fail?

It runs in three parts and never prints a passphrase or password:

  1. Pi checks: noisy and its Python packages, NetworkManager and wpa_supplicant, eth0 link,
     wlan0 present and not blocked, Wi-Fi country, the secrets file, leftovers from killed runs.
     With fix=true it repairs what it safely can: radio on, rfkill unblock, secrets file owner
     and mode, leftover connections and policy rules, the state directory.
  2. Config checks per slot: SSID and username set, passphrase or password present in
     secrets.json, and the SSID seen in a scan with the security the slot expects.
  3. A live test per slot (connect=true): bring the slot up exactly as a run does, then query
     each DNS server DHCP handed out and fetch http://connectivitycheck.gstatic.com/generate_204
     from the slot's address, then tear down. No noisy crawl, so a slot takes seconds, not minutes.

Each slot ends with PASS or FAIL, the stage that failed and a hint. With use_schedule_values=true
(the default) the SSIDs, usernames, VLANs and security come from this Pi's last scheduled run,
so the Run page needs no per-branch values.
"""
import json
import os
import random
import re
import socket
import stat
import struct
import subprocess
import time

import identities
import netmanage

TEST_HOST = "connectivitycheck.gstatic.com"
TEST_PATH = "/generate_204"
WIFI_SLOTS = (4, 5, 6, 7)

# Values a scheduled run saves for check mode (never secrets).
SCHEDULE_KEYS = ("slots", "vlan_a", "vlan_b", "ssid_sae", "ssid_owe", "owe_hidden", "psk_security",
                 "guest_security", "ssid_nac", "nac_user", "ssid_3pr", "tpr_user", "eap_device")


def save_run_params(state_dir):
    """Called by every normal run: remember this Pi's schedule values for check mode."""
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        vals = {k: os.environ[f"PARAM_{k.upper()}"] for k in SCHEDULE_KEYS if f"PARAM_{k.upper()}" in os.environ}
        vals["_saved_at"] = int(time.time())
        vals["_by"] = os.environ.get("PIMGR_CREATED_BY", "")
        (state_dir / "last_params.json").write_text(json.dumps(vals, indent=1) + "\n")
    except OSError:
        pass


def use_saved_params(state_dir, out):
    path = state_dir / "last_params.json"
    try:
        vals = json.loads(path.read_text())
    except (OSError, ValueError):
        out("  ! no saved schedule values on this Pi yet (they're saved by each scheduled run);"
            " using this job's parameters")
        return
    for k in SCHEDULE_KEYS:
        if k in vals:
            os.environ[f"PARAM_{k.upper()}"] = str(vals[k])
    age = (time.time() - vals.get("_saved_at", 0)) / 3600
    out(f"  using schedule values saved {age:.1f} h ago: "
        + ", ".join(f"{k}={vals[k]}" for k in SCHEDULE_KEYS if vals.get(k) not in (None, "")))


# --------------------------------------------------------------------------- helpers

def sh(cmd, timeout=30):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"


def read_secrets(path):
    """(data, problem). Never returns or prints values beyond the dict itself."""
    try:
        st = path.stat()
    except FileNotFoundError:
        return {}, "missing"
    problem = None
    if st.st_uid != 0 or st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        problem = f"owner uid {st.st_uid}, mode {oct(st.st_mode & 0o777)} (must be root, 600)"
    try:
        return json.loads(path.read_text()), problem
    except ValueError as exc:
        return {}, f"not valid JSON ({exc.msg}, line {exc.lineno})"


def secret_cmd(path, missing):
    """One command that prompts for every missing secret and writes the file root-only."""
    items = json.dumps(missing)
    return ("sudo python3 -c 'import json,getpass,os,pathlib;"
            f"p=pathlib.Path(\"{path}\");p.parent.mkdir(parents=True,exist_ok=True);"
            "d=json.loads(p.read_text()) if p.exists() else {};"
            f"[d.setdefault(k,{{}}).__setitem__(n,getpass.getpass(f\"{{k}} {{n}}: \")) for k,n in {items}];"
            "p.write_text(json.dumps(d,indent=1));os.chmod(p,0o600)'")


def dns_query(server, name, src_ip=None, timeout=3.0):
    """Minimal A lookup over UDP from src_ip. Returns (status, detail):
    ok/ip, nxdomain, servfail, refused, timeout, error."""
    qid = random.randint(0, 0xFFFF)
    q = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    q += b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0" + struct.pack(">HH", 1, 1)
    host, _, port = server.partition("#")
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        if src_ip:
            s.bind((src_ip, 0))
        s.sendto(q, (host, int(port or 53)))
        while True:
            data, _ = s.recvfrom(4096)
            if len(data) >= 12 and struct.unpack(">H", data[:2])[0] == qid:
                break
    except socket.timeout:
        return "timeout", f"no answer in {timeout:.0f}s"
    except OSError as exc:
        return "error", str(exc)
    finally:
        s.close()
    rcode = struct.unpack(">H", data[2:4])[0] & 0xF
    if rcode == 3:
        return "nxdomain", "NXDOMAIN"
    if rcode == 2:
        return "servfail", "SERVFAIL"
    if rcode == 5:
        return "refused", "REFUSED"
    if rcode:
        return "error", f"rcode {rcode}"
    ancount = struct.unpack(">H", data[6:8])[0]
    i = 12
    while data[i]:                       # question name
        i += 1 + data[i]
    i += 5
    for _ in range(ancount):
        if data[i] & 0xC0 == 0xC0:
            i += 2
        else:
            while data[i]:
                i += 1 + data[i]
            i += 1
        rtype, _, _, rdlen = struct.unpack(">HHIH", data[i:i + 10])
        i += 10
        if rtype == 1 and rdlen == 4:
            return "ok", socket.inet_ntoa(data[i:i + 4])
        i += rdlen
    return "error", "no A record in the answer"


def http_probe(ip, src_ip=None, timeout=8.0):
    """GET TEST_PATH from ip:80 (Host TEST_HOST). Returns (status, detail)."""
    try:
        with socket.create_connection((ip, 80), timeout=timeout,
                                      source_address=(src_ip, 0) if src_ip else None) as s:
            s.sendall(f"GET {TEST_PATH} HTTP/1.1\r\nHost: {TEST_HOST}\r\nConnection: close\r\n\r\n".encode())
            line = s.recv(256).split(b"\r\n", 1)[0].decode(errors="replace")
    except socket.timeout:
        return "timeout", f"no answer from {ip}:80 in {timeout:.0f}s"
    except OSError as exc:
        return "error", f"{ip}:80 {exc}"
    m = re.match(r"HTTP/\S+ (\d+)", line)
    if m and m.group(1) == "204":
        return "ok", "204"
    return "intercepted", line or "empty reply"


def journal_since(since):
    _, out, _ = sh(["journalctl", "-u", "wpa_supplicant", "-u", "NetworkManager", "--no-pager",
                    "-o", "cat", "--since", "@%d" % int(since)])
    return out


# --------------------------------------------------------------------------- hints

def classify_wifi_failure(profile, err, journal):
    """(stage, detail, hint) from the setup error and what wpa_supplicant logged."""
    ssid, sec = profile["ssid"], profile["wifi_security"]
    eap = sec == "wpa-eap"
    text = err + "\n" + journal
    if "could not be found" in text or "No network with SSID" in text:
        return ("scan", f"'{ssid}' not found",
                "The Pi can't see this SSID. Check it's enabled on this branch's APs, on 2.4 or 5 GHz "
                "(not 6 GHz only), and spelled exactly, leading dot included.")
    m = re.search(r"ASSOC-REJECT .*status_code=(\d+)", text)
    if m:
        return ("associate", f"AP rejected association (status {m.group(1)})",
                "Usually a security mismatch: compare the SSID's security (WPA2, WPA3, Transition, PMF) "
                "with the slot's psk_security/guest_security, or a NAC/MAC policy on the SSID.")
    if eap:
        user = profile["eap"]["identity"]
        server = ("Meter NAC (Dashboard: NAC users and auth events)" if profile["slot"] == 6
                  else "the third-party RADIUS, Lab Services (its auth log, user " + user + ")")
        if "EAP-FAILURE" in text or "EAP-STARTED" in text:
            return ("802.1X", f"EAP for '{user}' did not succeed",
                    f"Check the user and password at {server}. If its auth log has no entry for this "
                    "attempt, this branch's RADIUS requests aren't reaching it: check the SSID's RADIUS "
                    "servers, the NAD/client entry for this branch's source IP, and routing to the server.")
        return ("802.1X", f"no EAP exchange for '{user}'",
                f"The AP never started 802.1X. Check the SSID is WPA2-Enterprise with RADIUS pointing at {server}.")
    if re.search(r"WRONG_KEY|auth_type=3|4-Way Handshake failed|Secrets were required", text):
        return ("authenticate", "passphrase rejected",
                f"Re-enter psk['{ssid}'] in secrets.json. If the SSID is WPA3-only, set "
                f"{'psk_security' if profile['slot'] == 4 else 'guest_security'}=sae.")
    if "no DHCP lease" in text:
        return ("dhcp", "associated but no DHCP lease",
                "The SSID's VLAN has no DHCP scope or relay, or the DHCP server can't be reached.")
    return ("connect", err.splitlines()[-1][:200] if err else "failed", "See the wifi log lines above.")


def dns_hint(results):
    bad = [f"{srv} {st}" for srv, st, _ in results if st != "ok"]
    states = {st for _, st, _ in results if st != "ok"}
    if "timeout" in states:
        return ("A DNS server doesn't answer from this VLAN: a firewall rule or no route to it. "
                "Down: " + ", ".join(bad))
    if states <= {"servfail", "refused", "nxdomain"}:
        return ("The DNS server answers but can't resolve public names: give it an upstream (Meter "
                "gateway DNS settings, or a default forwarder in Lab Services). " + ", ".join(bad))
    return "DNS lookups failed: " + "; ".join(f"{srv} {det}" for srv, st, det in results if st != "ok")


# --------------------------------------------------------------------------- main

def run_check(*, profiles, net, slots, secrets_file, state_dir, out, log, fix, connect):
    results, problems, fixed = {}, [], []

    def note(ok, label, detail="", hint=""):
        out(f"  {'ok ' if ok else 'XX '} {label}" + (f": {detail}" if detail else ""))
        if not ok:
            problems.append(label)
            if hint:
                out(f"       -> {hint}")

    out("== Pi")
    py = os.environ.get("PIMGR_TOOLS_PY", "")
    note(os.path.exists("/opt/pimgr-tools/noisy/noisy.py"), "noisy installed", "/opt/pimgr-tools/noisy",
         "Assign the traffic-gen or lab-full profile to this Pi and apply it.")
    rc, _, err = sh([py or "python3", "-c", "import aiohttp, aiodns"])
    note(rc == 0, "noisy Python packages (aiohttp, aiodns)", err.strip().splitlines()[-1] if rc else py,
         "Re-apply the Pi's profile so the tools venv is rebuilt.")
    for svc in ("NetworkManager", "wpa_supplicant"):
        rc, o, _ = sh(["systemctl", "is-active", svc])
        note(o.strip() == "active", f"{svc} running", o.strip(), f"sudo systemctl restart {svc}")

    wired, wifi = net["wired_iface"], net["wifi_iface"]
    carrier = open(f"/sys/class/net/{wired}/carrier").read().strip() if os.path.exists(f"/sys/class/net/{wired}/carrier") else "0"
    note(carrier == "1", f"{wired} link", "up" if carrier == "1" else "no carrier", "Check the cable and switch port.")
    has_wifi = os.path.exists(f"/sys/class/net/{wifi}")
    note(has_wifi, f"{wifi} present", "", "No Wi-Fi interface: Wi-Fi slots 4-7 can't run on this Pi.")
    if has_wifi:
        rc, o, _ = sh(["nmcli", "radio", "wifi"])
        if o.strip() != "enabled" and fix:
            sh(["nmcli", "radio", "wifi", "on"])
            fixed.append("Wi-Fi radio turned on")
            rc, o, _ = sh(["nmcli", "radio", "wifi"])
        note(o.strip() == "enabled", "Wi-Fi radio on", o.strip(), "Run with fix=true, or: sudo nmcli radio wifi on")
        rc, o, _ = sh(["rfkill", "list", "wifi"])
        if "Soft blocked: yes" in o and fix:
            sh(["rfkill", "unblock", "wifi"])
            fixed.append("rfkill unblocked")
            rc, o, _ = sh(["rfkill", "list", "wifi"])
        note("blocked: yes" not in o, "Wi-Fi not blocked (rfkill)",
             "hard blocked" if "Hard blocked: yes" in o else ("soft blocked" if "Soft blocked: yes" in o else ""),
             "Run with fix=true, or: sudo rfkill unblock wifi")
        rc, o, _ = sh(["iw", "reg", "get"])
        m = re.search(r"country (\w\w)", o)
        note(bool(m) and m.group(1) != "00", "Wi-Fi country set", m.group(1) if m else "unknown",
             "Set it once: sudo raspi-config nonint do_wifi_country US")

    secrets, sproblem = read_secrets(secrets_file)
    if sproblem and sproblem.startswith("owner") and fix:
        os.chown(secrets_file, 0, 0)
        os.chmod(secrets_file, 0o600)
        fixed.append(f"{secrets_file} set to root, 600")
        secrets, sproblem = read_secrets(secrets_file)
    note(not sproblem, f"secrets file {secrets_file}", sproblem or
         f"{len(secrets.get('psk') or {})} passphrase(s), {len(secrets.get('eap') or {})} 802.1X password(s)",
         "Run with fix=true to fix the owner and mode." if sproblem and sproblem.startswith("owner") else "")

    _, o, _ = sh(["nmcli", "-g", "NAME", "connection", "show"])
    left = [n for n in o.split() if n.startswith(netmanage.STALE_CONNECTIONS_PREFIX)]
    _, rules, _ = sh(["ip", "rule", "show"])
    base = net["route_rule_priority_base"]
    stale_rules = [l for l in rules.splitlines()
                   if l.split(":")[0].isdigit() and base < int(l.split(":")[0]) <= base + 7]
    if (left or stale_rules) and fix:
        netmanage.cleanup_stale(net, list(range(1, 8)), log)
        fixed.append("leftover connections and policy rules removed")
        left, stale_rules = [], []
    note(not left and not stale_rules, "no leftovers from killed runs",
         ", ".join(left + [r.split(":")[0] for r in stale_rules]), "Run with fix=true to remove them.")
    if not state_dir.exists() and fix:
        state_dir.mkdir(parents=True, exist_ok=True)
        fixed.append(f"{state_dir} created")

    # ---- config and scan
    out("== Slots: configuration")
    scan = {}
    if has_wifi:
        sh(["nmcli", "device", "wifi", "rescan", "ifname", wifi])
        time.sleep(6)
        _, o, _ = sh(["nmcli", "-t", "-e", "yes", "-f", "SSID,CHAN,SIGNAL,SECURITY", "device", "wifi", "list", "ifname", wifi])
        for line in o.splitlines():
            parts = re.split(r"(?<!\\):", line)
            if len(parts) >= 4:
                name = parts[0].replace("\\:", ":")
                best = scan.get(name)
                if not best or int(parts[2] or 0) > int(best[1] or 0):
                    scan[name] = (parts[1], parts[2], parts[3])
    missing = []
    ready = {}
    for slot in slots:
        p = profiles[slot]
        label = f"slot {slot} {p['name']}"
        if p["kind"] == "wired":
            ready[slot] = p
            out(f"  ok  {label}: " + (f"{wired} untagged" if p.get("management") else f"{wired}.{p['vlan']}"))
            continue
        if not p["ssid"]:
            results[slot] = ("config", "no SSID set", "Set ssid_sae / ssid_owe / ssid_nac / ssid_3pr on the schedule.")
            out(f"  XX  {label}: no SSID set")
            continue
        issues = []
        if p["wifi_security"] == "wpa-eap":
            user = p["eap"]["identity"]
            if not user:
                issues.append(f"no username ({p['user_param']})")
            elif not (secrets.get("eap") or {}).get(user):
                issues.append(f"no password for '{user}' in secrets.json")
                missing.append(["eap", user])
            else:
                p["eap"]["password"] = secrets["eap"][user]
        elif p["wifi_security"] in ("wpa-psk", "sae"):
            if not (secrets.get("psk") or {}).get(p["ssid"]):
                issues.append(f"no passphrase for '{p['ssid']}' in secrets.json")
                missing.append(["psk", p["ssid"]])
            else:
                p["psk"] = secrets["psk"][p["ssid"]]
        seen = scan.get(p["ssid"])
        if seen:
            chan, sig, secflags = seen
            want = {"wpa-eap": "802.1X", "sae": "WPA3", "wpa-psk": "WPA2", "owe": "OWE"}[p["wifi_security"]]
            scan_note = f"seen ch {chan}, signal {sig}%, security '{secflags}'"
            if want not in secflags and not (want == "WPA2" and "WPA1" in secflags):
                issues.append(f"{scan_note}, but the slot uses {p['wifi_security']}")
        elif p.get("wifi_hidden"):
            scan_note = "hidden SSID, not listed in scans (the live test probes for it)"
        else:
            scan_note = "not seen in a scan"
            issues.append(scan_note)
        if issues:
            results[slot] = ("config", "; ".join(issues), "")
            out(f"  XX  {label} '{p['ssid']}': " + "; ".join(issues))
            if any(i.startswith("no ") for i in issues):
                continue
        else:
            out(f"  ok  {label} '{p['ssid']}': {scan_note}")
        ready[slot] = p
    if missing:
        out("  To add the missing secrets, run this on the Pi (it prompts for each one, nothing is echoed):")
        out("    " + secret_cmd(secrets_file, missing))

    # ---- live tests
    if connect and ready:
        out("== Slots: live test (connect, DHCP, DNS, HTTP); about 15-60 s per slot")
        previous = netmanage.wifi_active_connection(wifi, log) if has_wifi else ""
        quick = dict(net, wifi_connect_attempts=2, wifi_scan_wait=4)
        try:
            for slot, p in ready.items():
                out(f"-- slot {slot} {p['name']}")
                started, state = time.time(), None
                try:
                    try:
                        ident = identities.get_identity(p)
                        state = netmanage.setup(p, ident, quick, log)
                    except Exception as exc:
                        if p["kind"] == "wifi":
                            results[slot] = classify_wifi_failure(p, str(exc), journal_since(started))
                        else:
                            msg = str(exc).splitlines()[0][:200]
                            results[slot] = ("dhcp" if "DHCP" in msg else "connect", msg,
                                             f"Check VLAN {p['vlan']} is tagged on this Pi's switch port and has DHCP."
                                             if p.get("vlan") else "")
                        continue
                    ip = state["bind_ip"]
                    src = None if p.get("management") else ip
                    servers = [s for s in netmanage.run(["nmcli", "-g", "IP4.DNS", "device", "show", state["iface"]],
                                                        log, check=False, quiet=True).stdout.replace("|", "\n").split()]
                    dns = []
                    for srv in servers or ["(none)"]:
                        st, det = dns_query(srv, TEST_HOST, src) if servers else ("error", "DHCP gave no DNS server")
                        dns.append((srv, st, det))
                        out(f"   DNS {srv}: {st} {det if st != 'ok' else ''}".rstrip())
                    good = [det for _, st, det in dns if st == "ok"]
                    if not good:
                        results[slot] = ("dns", "no DNS server resolved " + TEST_HOST, dns_hint(dns))
                        continue
                    st, det = http_probe(good[0], src)
                    out(f"   HTTP {TEST_HOST}: {st} {det}")
                    if st == "ok":
                        partial = [d for d in dns if d[1] != "ok"]
                        results[slot] = ("pass", f"ip {ip}" + (f"; but {dns_hint(partial)}" if partial else ""), "")
                    elif st == "intercepted":
                        results[slot] = ("http", det, "Something on this VLAN answers for the internet: a captive "
                                         "portal or proxy. Check the VLAN's captive portal settings.")
                    else:
                        results[slot] = ("http", det, "DNS works but there's no web egress from this VLAN: "
                                         "check firewall rules for it.")
                finally:
                    try:
                        netmanage.teardown(state, log)
                    except Exception as exc:
                        out(f"   teardown failed: {exc}")
        finally:
            if has_wifi:
                netmanage.restore_wifi(previous, log)

    # ---- summary
    out("== Summary")
    for slot in slots:
        stage, detail, hint = results.get(slot, ("skipped", "not tested (connect=false)", ""))
        status = "PASS" if stage == "pass" else ("----" if stage == "skipped" else "FAIL")
        out(f"  slot {slot}  {status}  " + (f"[{stage}] " if status == "FAIL" else "") + detail)
        if hint and status == "FAIL":
            out(f"           -> {hint}")
    if problems:
        out("  Pi problems: " + "; ".join(problems))
    if fixed:
        out("  Fixed: " + "; ".join(fixed))
    summary = {"mode": "check", "pi_problems": problems, "fixed": fixed,
               "slots": {str(s): {"status": "pass" if results.get(s, ("",))[0] == "pass" else
                                  results.get(s, ("skipped",))[0],
                                  "detail": results.get(s, ("", ""))[1]} for s in slots}}
    print("PIMGR_RESULT " + json.dumps(summary), flush=True)
    failed = problems or any(v["status"] not in ("pass", "skipped") for v in summary["slots"].values())
    return 1 if failed else 0
