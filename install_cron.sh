#!/usr/bin/env bash
#
# install_cron.sh — add the noisy_multiface orchestrator to ROOT's crontab.
#
# Each firing runs the next interface in the 5-slot rotation, so after 5 runs
# every interface has been used once, then it repeats. Root is required because
# MAC cloning (nmcli) and policy routing (ip rule/route) need it.
#
# Cadence is fixed (every 30 min by default); the wrapper adds a random jitter
# and random run duration on top.
#
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"

# Change this cron expression to taste. */30 = every 30 minutes.
CRON_EXPR="${CRON_EXPR:-*/30 * * * *}"

CMD="cd $HERE && ./venv/bin/python noisy_multiface.py"
LINE="$CRON_EXPR $CMD  # noisy-multiface"

# Build the new crontab in a temp file so an empty/missing existing crontab
# (grep finding no matches, or `crontab -l` failing on a fresh machine) can't
# abort the script.
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
sudo crontab -l 2>/dev/null | grep -v '# noisy-multiface' > "$TMP" || true
echo "$LINE" >> "$TMP"
sudo crontab "$TMP"

echo ">> Installed root crontab entry:"
echo "     $LINE"
echo
echo ">> View:    sudo crontab -l"
echo ">> Remove:  sudo crontab -l | grep -v '# noisy-multiface' | sudo crontab -"
