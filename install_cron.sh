#!/usr/bin/env bash
#
# install_cron.sh — add the noisy_random wrapper to the user's crontab.
#
# Cron cannot fire at genuinely random times, so we run on a fixed cadence
# (every 30 min by default) and let noisy_random.py handle the randomness:
# it rolls a skip chance, sleeps a random jitter, then runs for a random
# duration. Net effect: irregular, human-looking bursts of traffic.
#
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"

# Change this cron expression to taste. */30 = every 30 minutes.
CRON_EXPR="${CRON_EXPR:-*/30 * * * *}"

CMD="cd $HERE && ./venv/bin/python noisy_random.py"
LINE="$CRON_EXPR $CMD  # noisy-random"

# Build the new crontab in a temp file so an empty/missing existing crontab
# (grep finding no matches, or `crontab -l` failing on a fresh machine) can't
# abort the script.
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
crontab -l 2>/dev/null | grep -v '# noisy-random' > "$TMP" || true
echo "$LINE" >> "$TMP"
crontab "$TMP"

echo ">> Installed crontab entry:"
echo "     $LINE"
echo
echo ">> View:    crontab -l"
echo ">> Remove:  crontab -l | grep -v '# noisy-random' | crontab -"
