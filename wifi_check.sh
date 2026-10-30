#!/usr/bin/env bash
#
# wifi_check.sh — audit whether the WiFi slot is (or was) actually connecting.
#
# Shows the live wlan0 state plus the WiFi outcomes from the multiface log, so you
# can tell at a glance whether slots 4/5 associated and got a DHCP lease, or how
# they failed. Run any time (no root needed for the read-only nmcli queries):
#
#     bash wifi_check.sh
#
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
LOG="${NOISY_LOG:-$HERE/noisy_multiface.log}"
WIFI_IFACE="${WIFI_IFACE:-wlan0}"

echo "=== live wlan0 state ($WIFI_IFACE) ==="
if command -v nmcli >/dev/null 2>&1; then
    nmcli -f GENERAL.STATE,IP4.ADDRESS,IP4.GATEWAY device show "$WIFI_IFACE" 2>/dev/null \
        || echo "  (could not query $WIFI_IFACE)"
    echo
    echo "=== associated AP (marked '*') ==="
    nmcli -f IN-USE,SSID,SIGNAL,SECURITY,CHAN device wifi list ifname "$WIFI_IFACE" 2>/dev/null \
        | awk 'NR==1 || /^\*/'
else
    echo "  nmcli not found"
fi

echo
echo "=== last 10 WiFi outcomes from the run log ==="
if [ -f "$LOG" ]; then
    # The wrapper logs one of these per WiFi run:
    #   'wifi OK on attempt N/M ...'        -> associated + got a DHCP lease
    #   'wifi attempt N/M: ...'             -> a single attempt failed (will retry)
    #   'wifi failed to come up after ...'  -> all attempts exhausted
    grep -E 'wifi OK|wifi attempt|wifi failed|holding wifi association' "$LOG" \
        | tail -n 10 || echo "  (no WiFi log lines yet)"
else
    echo "  log not found: $LOG"
fi
