#!/usr/bin/env bash
#
# install.sh — set up madereddy/noisy + the random-interval wrapper on a Pi.
#
# Run once on the Raspberry Pi from inside the noisy-pi directory:
#     bash install.sh
#
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

echo ">> Ensuring system packages (git, python3, venv)…"
if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update -y
    sudo apt-get install -y git python3 python3-venv python3-pip
fi

echo ">> Cloning / updating noisy…"
if [ -d "noisy/.git" ]; then
    git -C noisy pull --ff-only
else
    git clone https://github.com/madereddy/noisy.git noisy
fi

echo ">> Creating virtualenv…"
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r noisy/requirements.txt

chmod +x noisy_random.py

echo
echo ">> Done. Test it manually first:"
echo "     NOISY_JITTER_MAX_SEC=0 NOISY_RUN_MAX_SEC=60 ./venv/bin/python noisy_random.py"
echo "   then watch:  tail -f noisy_random.log"
echo
echo ">> To install the cron job, run:  bash $HERE/install_cron.sh"
