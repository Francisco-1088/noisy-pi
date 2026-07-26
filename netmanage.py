#!/usr/bin/env python3
"""
netmanage.py — bring interfaces up/down for a run and route noisy out of them.

For each spoofing profile we create a transient NetworkManager connection with a
cloned MAC, a DHCP hostname/vendor-class, and DHCP addressing, then set up
*policy routing* so that traffic sourced from that interface's IP egresses that
interface — WITHOUT changing the system default route. That keeps the Pi's
management/SSH path (the untagged eth0 default route) intact the whole time.

noisy is launched with `--local-addr <that IP>` (see the multiface patch), so its
sockets are matched by the `ip rule` and pushed out the right interface.

Requires root (nmcli MAC cloning + `ip rule`/`ip route`).
"""

import ipaddress
import re
import subprocess
import time


def run(cmd, log, check=True, quiet=False):
    """Run a command; log it; return CompletedProcess. Raises on failure if check."""
    if not quiet:
        log.info("exec: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        msg = "cmd failed (%d): %s\nstdout: %s\nstderr: %s" % (
            proc.returncode, " ".join(cmd), proc.stdout.strip(), proc.stderr.strip())
        if check:
            raise RuntimeError(msg)
        if not quiet:
            log.warning(msg)
    return proc


def _nmcli_get(field, iface, log):
    proc = run(["nmcli", "-g", field, "device", "show", iface], log, check=False, quiet=True)
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def _dhcp_gateway(iface, log):
    """Fallback gateway lookup from the DHCP lease (used when never-default is set)."""
    proc = run(["nmcli", "-g", "DHCP4.OPTION", "device", "show", iface],
               log, check=False, quiet=True)
    m = re.search(r"routers?\s*=\s*(\d+\.\d+\.\d+\.\d+)", proc.stdout)
    return m.group(1) if m else ""


def _read_ipv4(iface, log, tries=10, delay=2.0):
    """Return (ip, prefixlen, gateway) once DHCP has assigned an address."""
    for _ in range(tries):
        addr = _nmcli_get("IP4.ADDRESS", iface, log)   # e.g. "192.168.3.50/24"
        gw = _nmcli_get("IP4.GATEWAY", iface, log)      # e.g. "192.168.3.1"
        if addr and "/" in addr:
            ip, prefix = addr.split("/", 1)
            # With ipv4.never-default set, IP4.GATEWAY may be blank; recover it
            # from the DHCP lease so we can still build our private route table.
            if not gw:
                gw = _dhcp_gateway(iface, log)
            return ip, int(prefix), (gw or None)
        time.sleep(delay)
    return None, None, None


def _wifi_is_connected(iface, log):
    """True if the WiFi device currently has an active association."""
    state = _nmcli_get("GENERAL.STATE", iface, log)  # e.g. "100 (connected)"
    return "connected" in state and "disconnected" not in state


def _table_and_priority(net, slot):
    return net["route_table_base"] + slot, net["route_rule_priority_base"] + slot


def _clear_policy(ip, table, priority, log):
    # Remove any stale rule at our priority and flush our table.
    for _ in range(5):
        p = run(["ip", "rule", "del", "priority", str(priority)], log, check=False, quiet=True)
        if p.returncode != 0:
            break
    run(["ip", "route", "flush", "table", str(table)], log, check=False, quiet=True)


def _install_policy(ip, prefix, gw, iface, table, priority, log):
    network = ipaddress.ip_interface(f"{ip}/{prefix}").network.with_prefixlen
    _clear_policy(ip, table, priority, log)
    # Connected subnet + default route live in our private table only.
    run(["ip", "route", "add", network, "dev", iface, "src", ip, "table", str(table)],
        log, check=False)
    if gw:
        run(["ip", "route", "add", "default", "via", gw, "dev", iface, "table", str(table)],
            log, check=False)
    # Anything sourced from our IP uses that table.
    run(["ip", "rule", "add", "from", f"{ip}/32", "table", str(table), "priority", str(priority)],
        log)
    run(["ip", "route", "flush", "cache"], log, check=False, quiet=True)


def _apply_dhcp_identity(con, profile, ident, log):
    mods = [
        "ipv4.method", "auto",
        "ipv4.dhcp-hostname", ident["hostname"],
        "ipv4.dhcp-send-hostname", "yes",
        # Never let a spoofed lease install a default route: the system default
        # (untagged eth0) must stay put so Raspberry Pi Connect — which rides the
        # default route outbound — is never rerouted through a spoofed interface.
        "ipv4.never-default", "yes",
        "ipv6.method", "ignore",
        "connection.autoconnect", "no",
    ]
    if profile.get("dhcp_vendor_class"):
        mods += ["ipv4.dhcp-vendor-class-identifier", profile["dhcp_vendor_class"]]
    if profile.get("dhcp_client_id"):
        mods += ["ipv4.dhcp-client-id", profile["dhcp_client_id"]]
    run(["nmcli", "connection", "modify", con] + mods, log)


# --------------------------------------------------------------------------- #

def setup(profile, ident, net, log):
    """
    Bring the profile's interface up and set up egress. Returns a state dict with
    at least {"bind_ip": <ip>} plus teardown info. Raises RuntimeError on failure.
    """
    slot = profile["slot"]
    wired = net["wired_iface"]
    wifi = net["wifi_iface"]
    up_timeout = str(net.get("nmcli_up_timeout", 90))

    # Slot 1: real untagged management interface — no changes, no policy route.
    if profile.get("management"):
        ip, prefix, gw = _read_ipv4(wired, log, tries=3, delay=1.0)
        if not ip:
            raise RuntimeError(f"could not read IPv4 for management iface {wired}")
        log.info("management interface %s ip=%s (no spoofing)", wired, ip)
        return {"bind_ip": ip, "profile": profile["name"], "policy": None,
                "con": None, "iface": wired}

    table, priority = _table_and_priority(net, slot)

    if profile["kind"] == "wired":
        iface = f"{wired}.{profile['vlan']}"
        con = f"noisy-vlan{profile['vlan']}"
        run(["nmcli", "connection", "delete", con], log, check=False, quiet=True)
        run(["nmcli", "connection", "add", "type", "vlan", "con-name", con,
             "ifname", iface, "dev", wired, "id", str(profile["vlan"])], log)
        run(["nmcli", "connection", "modify", con,
             "802-3-ethernet.cloned-mac-address", ident["mac"],
             "ipv4.route-metric", "700"], log)
        _apply_dhcp_identity(con, profile, ident, log)

    elif profile["kind"] == "wifi":
        iface = wifi
        con = "noisy-wifi"
        run(["nmcli", "connection", "delete", con], log, check=False, quiet=True)
        # Gracefully disconnect whatever wlan0 is currently associated with
        # before we take the radio over for this SSID (proper deauth, not a
        # yanked link).
        if _wifi_is_connected(wifi, log):
            log.info("wlan0 is associated; disconnecting gracefully before takeover")
            run(["nmcli", "device", "disconnect", wifi], log, check=False)
        run(["nmcli", "device", "wifi", "rescan"], log, check=False, quiet=True)
        run(["nmcli", "connection", "add", "type", "wifi", "con-name", con,
             "ifname", wifi, "ssid", profile["ssid"]], log)
        sec = profile["wifi_security"]
        if sec == "wpa-psk":
            run(["nmcli", "connection", "modify", con,
                 "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", profile["psk"]], log)
        elif sec == "sae":
            # WPA3-Personal (SAE). The SSID is WPA3-Transition (WPA2/WPA3 mixed),
            # and a WPA3-capable client like an iPhone negotiates SAE. pmf
            # "optional" keeps association robust across transition-mode APs.
            run(["nmcli", "connection", "modify", con,
                 "wifi-sec.key-mgmt", "sae", "wifi-sec.psk", profile["psk"],
                 "wifi-sec.pmf", "optional"], log)
        elif sec == "owe":
            run(["nmcli", "connection", "modify", con, "wifi-sec.key-mgmt", "owe"], log)
        else:
            raise RuntimeError(f"unknown wifi_security {sec!r}")
        run(["nmcli", "connection", "modify", con,
             "802-11-wireless.cloned-mac-address", ident["mac"],
             "ipv4.route-metric", "720"], log)
        _apply_dhcp_identity(con, profile, ident, log)
    else:
        raise RuntimeError(f"unknown profile kind {profile['kind']!r}")

    run(["nmcli", "--wait", up_timeout, "connection", "up", con], log)
    log.info("connection %s up as %s (mac=%s host=%s)", con, iface, ident["mac"], ident["hostname"])

    ip, prefix, gw = _read_ipv4(iface, log)
    if not ip:
        raise RuntimeError(f"{con}: no DHCP address on {iface} after activation")
    log.info("%s got ip=%s/%s gw=%s", iface, ip, prefix, gw)

    _install_policy(ip, prefix, gw, iface, table, priority, log)
    return {"bind_ip": ip, "profile": profile["name"], "iface": iface, "con": con,
            "policy": {"ip": ip, "table": table, "priority": priority}}


def teardown(state, log):
    if not state:
        return
    policy = state.get("policy")
    if policy:
        _clear_policy(policy["ip"], policy["table"], policy["priority"], log)
    con = state.get("con")
    if con:
        run(["nmcli", "connection", "down", con], log, check=False, quiet=True)
        run(["nmcli", "connection", "delete", con], log, check=False, quiet=True)
    log.info("torn down %s", state.get("profile"))
