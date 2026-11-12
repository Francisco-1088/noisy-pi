#!/usr/bin/env bash
# Pi Script Manager entrypoint (see manifest.json). One noisy run for duration_s, then exits.
# The noisy crawler itself comes from the `traffic-gen` profile (pinned, checksum-verified).
set -euo pipefail
NOISY=/opt/pimgr-tools/noisy/noisy.py
if [[ ! -f $NOISY || ! -x ${PIMGR_TOOLS_PY:-} ]]; then
  echo "noisy is not installed on this Pi: assign the traffic-gen (or lab-full) profile" >&2
  exit 2
fi
# On a policy-routed test interface (wlan0), traffic and DNS are bound to its address and use
# its own DNS servers. eth0 has no table of its own: it is the default route already.
table_var="PIMGR_TABLE_${PARAM_IFACE^^}"
if [[ -n ${!table_var:-} ]]; then
  [[ -n ${PARAM_IFACE_IP:-} ]] || { echo "$PARAM_IFACE has no IPv4 address (not connected?)" >&2; exit 2; }
  export NOISY_BIND_IP=$PARAM_IFACE_IP
  NOISY_DNS="$(nmcli -g IP4.DNS device show "$PARAM_IFACE" 2>/dev/null | tr '|' ' ' | xargs || true)"
  export NOISY_DNS
fi
exec "$PIMGR_TOOLS_PY" "$(dirname "$0")/pimgr_noisy.py" "$NOISY"
