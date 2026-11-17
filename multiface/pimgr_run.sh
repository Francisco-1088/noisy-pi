#!/usr/bin/env bash
# Pi Script Manager entrypoint for noisy-multiface (see manifest.json and pimgr_multiface.py).
# Runs as root: it creates VLAN and Wi-Fi connections with cloned MACs and policy routes.
# The crawler itself runs as the unprivileged job user.
set -euo pipefail
if [[ ! -f /opt/pimgr-tools/noisy/noisy.py || ! -x ${PIMGR_TOOLS_PY:-} ]]; then
  echo "noisy is not installed on this Pi: assign the traffic-gen (or lab-full) profile" >&2
  exit 2
fi
exec "$PIMGR_TOOLS_PY" "$(dirname "$0")/pimgr_multiface.py"
